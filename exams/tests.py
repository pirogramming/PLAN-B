import datetime
import io
import pypdf
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile

from .models import ExamPeriod, Exam, AvailableTime, StudyMaterial, StudyTask
from core.choices import (
    ExamPeriodStatus,
    MaterialStatus,
    MaterialType,
    TaskType,
    TaskDifficulty,
)
from core.exceptions import AICallFailedError, AIResponseValidationError
from exams.services.analysis_orchestrator import (
    AnalysisNotSupportedError,
    AnalysisPipelineError,
    DuplicateAnalysisRequestError,
    MAX_RETRY_COUNT,
    RetryLimitExceededError,
    analyze_and_estimate,
    get_analysis_status,
    retry_analysis,
)

from django.core.files.uploadedfile import SimpleUploadedFile
from exams.services import task_extractor
from exams.services.pdf_extractor import extract_text_from_pdf, PdfExtractionError

User = get_user_model()


class OwnershipTests(TestCase):
    """타 사용자 소유 객체(시험기간·과목·학습자료) 접근 차단 확인"""

    def setUp(self):
        self.owner = User.objects.create_user(
            username='owner@example.com', email='owner@example.com', password='pass1234!'
        )
        self.other = User.objects.create_user(
            username='other@example.com', email='other@example.com', password='pass1234!'
        )
        self.period = ExamPeriod.objects.create(
            user=self.owner, title='2026 중간고사',
            start_date=datetime.date(2026, 10, 1), end_date=datetime.date(2026, 10, 10),
            status=ExamPeriodStatus.ACTIVE,
        )
        self.exam = Exam.objects.create(
            exam_period=self.period, subject_name='신호및시스템',
            exam_date=datetime.date(2026, 10, 5),
        )
        self.material = StudyMaterial.objects.create(
            exam=self.exam, title='1~3장 정리', material_type=MaterialType.TEXT,
            extracted_text='내용', status=MaterialStatus.COMPLETED,
        )

    def test_other_user_cannot_access_period_detail(self):
        self.client.force_login(self.other)
        response = self.client.get(reverse('exams:period_detail', args=[self.period.id]))
        self.assertEqual(response.status_code, 404)

    def test_other_user_cannot_access_subject_update(self):
        self.client.force_login(self.other)
        response = self.client.get(reverse('exams:subject_update', args=[self.period.id, self.exam.id]))
        self.assertEqual(response.status_code, 404)

    def test_other_user_cannot_access_material_detail(self):
        self.client.force_login(self.other)
        response = self.client.get(reverse('exams:material_detail', args=[self.material.id]))
        self.assertEqual(response.status_code, 404)

    def test_owner_can_access_own_resources(self):
        self.client.force_login(self.owner)
        self.assertEqual(
            self.client.get(reverse('exams:period_detail', args=[self.period.id])).status_code, 200
        )
        self.assertEqual(
            self.client.get(reverse('exams:material_detail', args=[self.material.id])).status_code, 200
        )


class PeriodCreateUpdateTests(TestCase):
    """시험기간 생성/수정, 축소 시 AvailableTime 정리"""

    def setUp(self):
        self.user = User.objects.create_user(
            username='u@example.com', email='u@example.com', password='pass1234!'
        )
        self.client.force_login(self.user)

    def test_period_create_success(self):
        response = self.client.post(reverse('exams:period_create'), {
            'title': '2026 중간고사',
            'start_date': '2026-10-01',
            'end_date': '2026-10-10',
        })
        self.assertEqual(response.status_code, 302)
        period = ExamPeriod.objects.get(user=self.user)
        self.assertEqual(period.status, ExamPeriodStatus.ACTIVE)
        self.assertEqual(AvailableTime.objects.filter(exam_period=period).count(), 10)

    def test_period_update_success(self):
        period = ExamPeriod.objects.create(
            user=self.user, title='기존',
            start_date=datetime.date(2026, 10, 1), end_date=datetime.date(2026, 10, 10),
            status=ExamPeriodStatus.ACTIVE,
        )
        response = self.client.post(reverse('exams:period_update', args=[period.id]), {
            'title': '수정된 제목',
            'start_date': '2026-10-01',
            'end_date': '2026-10-10',
        })
        self.assertEqual(response.status_code, 302)
        period.refresh_from_db()
        self.assertEqual(period.title, '수정된 제목')

    def test_period_shrink_removes_out_of_range_available_time(self):
        period = ExamPeriod.objects.create(
            user=self.user, title='기존',
            start_date=datetime.date(2026, 10, 1), end_date=datetime.date(2026, 10, 10),
            status=ExamPeriodStatus.ACTIVE,
        )
        for i in range(10):
            AvailableTime.objects.create(
                exam_period=period,
                date=datetime.date(2026, 10, 1) + datetime.timedelta(days=i),
                available_minutes=60,
            )
        self.assertEqual(AvailableTime.objects.filter(exam_period=period).count(), 10)

        response = self.client.post(reverse('exams:period_update', args=[period.id]), {
            'title': '기존',
            'start_date': '2026-10-01',
            'end_date': '2026-10-05',
        })
        self.assertEqual(response.status_code, 302)

        remaining = AvailableTime.objects.filter(exam_period=period)
        self.assertEqual(remaining.count(), 5)
        self.assertTrue(all(
            datetime.date(2026, 10, 1) <= at.date <= datetime.date(2026, 10, 5)
            for at in remaining
        ))
        self.assertTrue(all(at.available_minutes == 60 for at in remaining))


class ExamModelTests(TestCase):
    def test_exam_str_representation(self):
        user = User.objects.create_user(
            username='u2@example.com', email='u2@example.com', password='pass1234!'
        )
        period = ExamPeriod.objects.create(
            user=user, title='기간',
            start_date=datetime.date(2026, 10, 1), end_date=datetime.date(2026, 10, 10),
        )
        exam = Exam.objects.create(
            exam_period=period, subject_name='공학수학', exam_date=datetime.date(2026, 10, 8),
        )
        self.assertEqual(str(exam), f"공학수학 ({exam.exam_date})")


class MaterialCreateTests(TestCase):
    """자료 등록 시 입력 유형별 status 처리"""

    def setUp(self):
        self.user = User.objects.create_user(
            username='u3@example.com', email='u3@example.com', password='pass1234!'
        )
        self.client.force_login(self.user)
        self.period = ExamPeriod.objects.create(
            user=self.user, title='기간',
            start_date=datetime.date(2026, 10, 1), end_date=datetime.date(2026, 10, 10),
        )
        self.exam = Exam.objects.create(
            exam_period=self.period, subject_name='과목',
            exam_date=datetime.date(2026, 10, 5),
        )

    def test_text_material_status_is_completed(self):
        response = self.client.post(reverse('exams:material_create', args=[self.exam.id]), {
            'title': '1~3장 정리',
            'material_type': MaterialType.TEXT,
            'extracted_text': '텍스트로 직접 입력한 시험 범위입니다.',
        })
        self.assertEqual(response.status_code, 302)
        material = StudyMaterial.objects.get(exam=self.exam)
        self.assertEqual(material.status, MaterialStatus.COMPLETED)
        # AI 분석 상태는 건드리지 않고 기본값(PENDING) 유지
        self.assertEqual(material.analysis_status, MaterialStatus.PENDING)

    def test_pdf_material_status_stays_pending_until_extracted(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        pdf_file = SimpleUploadedFile("dummy.pdf", b"%PDF-1.4 dummy content", content_type="application/pdf")

        response = self.client.post(reverse('exams:material_create', args=[self.exam.id]), {
            'title': 'PDF 자료',
            'material_type': MaterialType.PDF,
            'file': pdf_file,
        })
        self.assertEqual(response.status_code, 302)
        material = StudyMaterial.objects.get(exam=self.exam)
        self.assertEqual(material.status, MaterialStatus.PENDING)


class MaterialExtractTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='u4@example.com', email='u4@example.com', password='pass1234!'
        )
        self.client.force_login(self.user)
        self.period = ExamPeriod.objects.create(
            user=self.user, title='기간',
            start_date=datetime.date(2026, 10, 1), end_date=datetime.date(2026, 10, 10),
        )
        self.exam = Exam.objects.create(
            exam_period=self.period, subject_name='과목',
            exam_date=datetime.date(2026, 10, 5),
        )

    def _make_pdf_material(self):
        dummy_pdf = SimpleUploadedFile(
            "dummy.pdf", b"%PDF-1.4 dummy content", content_type="application/pdf"
        )
        return StudyMaterial.objects.create(
            exam=self.exam, title='자료', material_type=MaterialType.PDF,
            file=dummy_pdf, status=MaterialStatus.PENDING,
        )

    @patch('exams.views.extract_text_from_pdf')
    def test_extract_success(self, mock_extract):
        mock_extract.return_value = "추출된 텍스트입니다."
        material = self._make_pdf_material()

        response = self.client.post(reverse('exams:material_extract', args=[material.id]))
        material.refresh_from_db()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(material.status, MaterialStatus.COMPLETED)
        self.assertEqual(material.extracted_text, "추출된 텍스트입니다.")

    @patch('exams.views.extract_text_from_pdf')
    def test_extract_empty_text_marks_failed(self, mock_extract):
        mock_extract.return_value = ""
        material = self._make_pdf_material()

        response = self.client.post(reverse('exams:material_extract', args=[material.id]))
        material.refresh_from_db()

        self.assertEqual(material.status, MaterialStatus.FAILED)
        self.assertIn("스캔", material.error_message)

    @patch('exams.views.extract_text_from_pdf')
    def test_extract_success_resets_stale_analysis_state(self, mock_extract):
        """
        이전 텍스트 기준으로 FAILED였던 AI 분석 상태가, 재추출 성공(=새 텍스트로
        교체) 후에는 PENDING/재시도횟수 0으로 초기화되어야 한다 (새 텍스트니까
        최초 분석부터 다시 시작할 수 있어야 함).
        """
        mock_extract.return_value = "새로 추출된 텍스트입니다."
        material = self._make_pdf_material()
        material.status = MaterialStatus.COMPLETED
        material.extracted_text = "예전 텍스트"
        material.analysis_status = MaterialStatus.FAILED
        material.analysis_error_message = "예전 텍스트 기준 실패 사유"
        material.analysis_retry_count = 1
        material.save(update_fields=[
            "status", "extracted_text", "analysis_status",
            "analysis_error_message", "analysis_retry_count",
        ])

        self.client.post(reverse('exams:material_extract', args=[material.id]))
        material.refresh_from_db()

        self.assertEqual(material.extracted_text, "새로 추출된 텍스트입니다.")
        self.assertEqual(material.analysis_status, MaterialStatus.PENDING)
        self.assertIsNone(material.analysis_error_message)
        self.assertEqual(material.analysis_retry_count, 0)

    @patch('exams.views.extract_text_from_pdf')
    def test_extract_success_keeps_analysis_state_when_text_unchanged(self, mock_extract):
        """
        리뷰 반영: 재추출 결과가 기존 텍스트와 완전히 같다면(예: 같은 PDF를 실수로
        다시 업로드), AI 분석 상태를 초기화하면 안 된다. 무조건 초기화하면 재시도
        횟수를 이미 다 쓴 자료도 같은 PDF를 다시 추출하는 것만으로
        analysis_retry_count가 0으로 리셋되어 재시도 제한을 우회할 수 있다.
        """
        mock_extract.return_value = "변하지 않는 텍스트입니다."
        material = self._make_pdf_material()
        material.status = MaterialStatus.COMPLETED
        material.extracted_text = "변하지 않는 텍스트입니다."
        material.analysis_status = MaterialStatus.FAILED
        material.analysis_error_message = "예전 실패 사유"
        material.analysis_retry_count = MAX_RETRY_COUNT
        material.save(update_fields=[
            "status", "extracted_text", "analysis_status",
            "analysis_error_message", "analysis_retry_count",
        ])

        self.client.post(reverse('exams:material_extract', args=[material.id]))
        material.refresh_from_db()

        self.assertEqual(material.extracted_text, "변하지 않는 텍스트입니다.")
        self.assertEqual(material.analysis_status, MaterialStatus.FAILED)
        self.assertEqual(material.analysis_error_message, "예전 실패 사유")
        self.assertEqual(material.analysis_retry_count, MAX_RETRY_COUNT)

    @patch('exams.views.extract_text_from_pdf')
    def test_extract_blocked_when_analysis_processing(self, mock_extract):
        """
        AI 분석이 진행 중인 자료는 재추출하면 안 된다 - 어느 텍스트 기준으로
        분석 중인지 꼬일 수 있기 때문 (analysis_orchestrator 쪽 재검증과 짝을 이룸).
        """
        material = self._make_pdf_material()
        material.status = MaterialStatus.COMPLETED
        material.extracted_text = "기존 추출 텍스트"
        material.analysis_status = MaterialStatus.PROCESSING
        material.save(update_fields=["status", "extracted_text", "analysis_status"])

        response = self.client.post(
            reverse('exams:material_extract', args=[material.id]), follow=True
        )

        material.refresh_from_db()
        mock_extract.assert_not_called()
        self.assertEqual(material.extracted_text, "기존 추출 텍스트")
        messages_list = list(response.context['messages'])
        self.assertTrue(any("AI 분석이 진행 중인" in str(m) for m in messages_list))

    @patch('exams.views.extract_text_from_pdf')
    def test_extract_blocked_when_analysis_completed(self, mock_extract):
        """AI 분석이 이미 끝난 자료도 재추출하면 안 된다 (결과가 옛 텍스트 기준이 됨)."""
        material = self._make_pdf_material()
        material.status = MaterialStatus.COMPLETED
        material.extracted_text = "기존 추출 텍스트"
        material.analysis_status = MaterialStatus.COMPLETED
        material.save(update_fields=["status", "extracted_text", "analysis_status"])

        response = self.client.post(
            reverse('exams:material_extract', args=[material.id]), follow=True
        )

        material.refresh_from_db()
        mock_extract.assert_not_called()
        self.assertEqual(material.extracted_text, "기존 추출 텍스트")
        messages_list = list(response.context['messages'])
        self.assertTrue(any("이미 AI 분석이 완료된" in str(m) for m in messages_list))

class StudyTaskCreateTests(TestCase):
    """직접 추가한 학습 작업의 예상시간 계산"""

    def setUp(self):
        self.user = User.objects.create_user(
            username='u5@example.com', email='u5@example.com', password='pass1234!'
        )
        self.client.force_login(self.user)
        self.period = ExamPeriod.objects.create(
            user=self.user, title='기간',
            start_date=datetime.date(2026, 10, 1), end_date=datetime.date(2026, 10, 10),
        )
        self.exam = Exam.objects.create(
            exam_period=self.period, subject_name='과목',
            exam_date=datetime.date(2026, 10, 5), speed_factor=1.2,
        )

    @patch('exams.views.estimate_task_minutes')
    def test_directly_added_task_has_positive_estimated_time(self, mock_estimate):
        mock_estimate.return_value = (40, 60)

        response = self.client.post(reverse('exams:task_create', args=[self.exam.id]), {
            'unit_name': '1장',
            'title': '개념 정리',
            'task_type': TaskType.CONCEPT,
            'importance': 'medium',
            'depth': 'basic',
            'difficulty': TaskDifficulty.NORMAL,
        })

        self.assertEqual(response.status_code, 302)
        task = StudyTask.objects.get(exam=self.exam)
        self.assertGreater(task.estimated_min_minutes, 0)
        self.assertGreater(task.estimated_max_minutes, 0)
        self.assertTrue(task.is_user_modified)
        mock_estimate.assert_called_once_with(
            task_type=TaskType.CONCEPT,
            difficulty=TaskDifficulty.NORMAL,
            speed_factor=1.2,
        )

class TaskReviewTests(TestCase):
    """작업 수정 시 예상시간 재계산 확인"""

    def setUp(self):
        self.user = User.objects.create_user(
            username='u6@example.com', email='u6@example.com', password='pass1234!'
        )
        self.client.force_login(self.user)
        self.period = ExamPeriod.objects.create(
            user=self.user, title='기간',
            start_date=datetime.date(2026, 10, 1), end_date=datetime.date(2026, 10, 10),
        )
        self.exam = Exam.objects.create(
            exam_period=self.period, subject_name='과목',
            exam_date=datetime.date(2026, 10, 5), speed_factor=1.0,
        )
        self.task = StudyTask.objects.create(
            exam=self.exam, unit_name='1장', title='개념 정리',
            task_type=TaskType.CONCEPT, difficulty=TaskDifficulty.EASY,
            estimated_min_minutes=20, estimated_max_minutes=30,
        )

    @patch('exams.views.estimate_task_minutes')
    def test_task_update_recalculates_estimated_time(self, mock_estimate):
        # difficulty를 EASY→HARD로 바꾸면 더 큰 값이 반환된다고 가정
        mock_estimate.return_value = (80, 120)

        management_form_data = {
            'form-TOTAL_FORMS': '1',
            'form-INITIAL_FORMS': '1',
            'form-MIN_NUM_FORMS': '0',
            'form-MAX_NUM_FORMS': '1000',
            'form-0-id': self.task.id,
            'form-0-unit_name': self.task.unit_name,
            'form-0-title': self.task.title,
            'form-0-task_type': TaskType.CONCEPT,
            'form-0-importance': 'medium',
            'form-0-depth': 'basic',
            'form-0-difficulty': TaskDifficulty.HARD,  # EASY → HARD로 수정
        }
        response = self.client.post(
            reverse('exams:task_review', args=[self.exam.id]), management_form_data
        )
        self.assertEqual(response.status_code, 302)

        self.task.refresh_from_db()
        self.assertEqual(self.task.difficulty, TaskDifficulty.HARD)
        self.assertEqual(self.task.estimated_min_minutes, 80)
        self.assertEqual(self.task.estimated_max_minutes, 120)
        mock_estimate.assert_called_once_with(
            task_type=TaskType.CONCEPT,
            difficulty=TaskDifficulty.HARD,
            speed_factor=1.0,
        )

class AnalysisOrchestratorTestCase(TestCase):
    """
    analysis_orchestrator.py 리뷰 확정 사항 검증:
    - 상태 필드 분리(status/error_message vs analysis_status/analysis_error_message)
    - 최초 분석/재시도 상태 전이, 재시도 횟수 제한(최대 2회)
    - 파이프라인 전체 예외 처리 및 롤백
    - 빈 결과(0개) 실패 처리
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="orch_tester@example.com", email="orch_tester@example.com", password="pass1234!"
        )
        self.period = ExamPeriod.objects.create(
            user=self.user, title="테스트 시험기간",
            start_date=datetime.date(2026, 8, 1), end_date=datetime.date(2026, 8, 20),
        )
        self.exam = Exam.objects.create(
            exam_period=self.period, subject_name="테스트과목", exam_date=datetime.date(2026, 8, 18),
        )

    def _make_material(self, text="1장 개념 정리"):
        return StudyMaterial.objects.create(
            exam=self.exam, title="테스트 자료", extracted_text=text,
            status=MaterialStatus.COMPLETED,
        )

    # ---------- 최초 분석 ----------

    def test_initial_analysis_success_sets_completed(self):
        material = self._make_material()
        tasks = analyze_and_estimate(material)

        material.refresh_from_db()
        self.assertTrue(len(tasks) > 0)
        self.assertEqual(material.analysis_status, MaterialStatus.COMPLETED)
        self.assertIsNone(material.analysis_error_message)

    def test_initial_analysis_does_not_increment_retry_count(self):
        material = self._make_material()
        analyze_and_estimate(material)

        material.refresh_from_db()
        self.assertEqual(material.analysis_retry_count, 0)

    def test_initial_analysis_does_not_touch_extraction_status_fields(self):
        material = self._make_material()
        material.status = MaterialStatus.COMPLETED
        material.error_message = None
        material.save(update_fields=["status", "error_message"])

        analyze_and_estimate(material)

        material.refresh_from_db()
        # 텍스트 추출 상태(status/error_message)는 AI 분석과 무관하게 그대로 유지돼야 한다
        self.assertEqual(material.status, MaterialStatus.COMPLETED)
        self.assertIsNone(material.error_message)

    @patch("exams.services.analysis_orchestrator.fetch_extracted_tasks")
    def test_ai_analysis_failure_sets_failed_and_rolls_back(self, mock_analyze):
        mock_analyze.side_effect = AICallFailedError("AI 서버 연결 실패")
        material = self._make_material()

        with self.assertRaises(AICallFailedError):
            analyze_and_estimate(material)

        material.refresh_from_db()
        self.assertEqual(material.analysis_status, MaterialStatus.FAILED)
        self.assertEqual(material.analysis_error_message, "AI 서버 연결 실패")
        self.assertEqual(StudyTask.objects.filter(study_material=material).count(), 0)

    @patch("exams.services.analysis_orchestrator.estimate_task_minutes")
    def test_time_estimation_failure_sets_failed_and_rolls_back(self, mock_estimate):
        mock_estimate.side_effect = ValueError("예상시간 계산 중 알 수 없는 오류")
        material = self._make_material()

        # 예기치 못한 예외(ValueError)는 AnalysisPipelineError로 변환되어 발생한다
        # (View가 AIAnalysisError/AnalysisPipelineError만 알면 되도록 하기 위함)
        with self.assertRaises(AnalysisPipelineError):
            analyze_and_estimate(material)

        material.refresh_from_db()
        self.assertEqual(material.analysis_status, MaterialStatus.FAILED)
        # 예기치 못한 예외는 상세 내용을 그대로 노출하지 않고 일반 문구로 저장한다
        self.assertNotIn("예상시간 계산 중 알 수 없는 오류", material.analysis_error_message or "")
        self.assertEqual(StudyTask.objects.filter(study_material=material).count(), 0)

    @patch("exams.services.analysis_orchestrator.fetch_extracted_tasks")
    def test_empty_result_is_treated_as_failure(self, mock_analyze):
        mock_analyze.return_value = []
        material = self._make_material()

        with self.assertRaises(AIResponseValidationError):
            analyze_and_estimate(material)

        material.refresh_from_db()
        self.assertEqual(material.analysis_status, MaterialStatus.FAILED)

    def test_duplicate_request_rejected_while_processing(self):
        material = self._make_material()
        material.analysis_status = MaterialStatus.PROCESSING
        material.save(update_fields=["analysis_status"])

        with self.assertRaises(DuplicateAnalysisRequestError):
            analyze_and_estimate(material)

    # ---------- 재시도 ----------

    def test_retry_rejected_when_completed(self):
        material = self._make_material()
        material.analysis_status = MaterialStatus.COMPLETED
        material.save(update_fields=["analysis_status"])

        with self.assertRaises(AnalysisNotSupportedError):
            retry_analysis(material)

    def test_first_retry_succeeds_from_failed(self):
        material = self._make_material()
        material.analysis_status = MaterialStatus.FAILED
        material.analysis_retry_count = 0
        material.save(update_fields=["analysis_status", "analysis_retry_count"])

        tasks = retry_analysis(material)

        material.refresh_from_db()
        self.assertTrue(len(tasks) > 0)
        self.assertEqual(material.analysis_status, MaterialStatus.COMPLETED)
        self.assertEqual(material.analysis_retry_count, 1)

    def test_second_retry_succeeds_after_first_retry_fails(self):
        material = self._make_material()
        material.analysis_status = MaterialStatus.FAILED
        material.analysis_retry_count = 0
        material.save(update_fields=["analysis_status", "analysis_retry_count"])

        # 1차 재시도는 실패시킨다
        with patch("exams.services.analysis_orchestrator.fetch_extracted_tasks") as mock_analyze:
            mock_analyze.side_effect = AICallFailedError("1차 재시도 실패")
            with self.assertRaises(AICallFailedError):
                retry_analysis(material)

        material.refresh_from_db()
        self.assertEqual(material.analysis_retry_count, 1)
        self.assertEqual(material.analysis_status, MaterialStatus.FAILED)

        # 2차 재시도는 mock 모드 기본 흐름 그대로 성공시킨다
        tasks = retry_analysis(material)

        material.refresh_from_db()
        self.assertTrue(len(tasks) > 0)
        self.assertEqual(material.analysis_status, MaterialStatus.COMPLETED)
        self.assertEqual(material.analysis_retry_count, 2)

    def test_retry_blocked_after_max_retry_count_reached(self):
        material = self._make_material()
        material.analysis_status = MaterialStatus.FAILED
        material.analysis_retry_count = MAX_RETRY_COUNT
        material.save(update_fields=["analysis_status", "analysis_retry_count"])

        with self.assertRaises(RetryLimitExceededError):
            retry_analysis(material)

    def test_retry_increments_retry_count_exactly_once(self):
        material = self._make_material()
        material.analysis_status = MaterialStatus.FAILED
        material.analysis_retry_count = 0
        material.save(update_fields=["analysis_status", "analysis_retry_count"])

        retry_analysis(material)

        material.refresh_from_db()
        self.assertEqual(material.analysis_retry_count, 1)

    @patch("exams.services.task_extractor._call_ai")
    def test_internal_self_correction_retry_does_not_affect_user_retry_count(self, mock_call_ai):
        """
        task_extractor 내부의 JSON 검증 self-correction 재요청(1회 실패 후 성공)이
        analysis_retry_count에는 영향을 주지 않아야 한다.
        """
        mock_call_ai.side_effect = [
            "이건 유효하지 않은 JSON 입니다",  # 1차 응답: 검증 실패 -> 내부 self-correction 유발
            '{"tasks": [{"unit_name": "1장", "title": "개념 읽기", "task_type": "concept", '
            '"importance": "high", "depth": "core", "difficulty": "normal", '
            '"ai_reason": "기초 개념이라 우선순위가 높습니다."}]}',
        ]
        material = self._make_material()

        tasks = analyze_and_estimate(material)

        material.refresh_from_db()
        self.assertTrue(len(tasks) > 0)
        self.assertEqual(material.analysis_status, MaterialStatus.COMPLETED)
        self.assertEqual(material.analysis_retry_count, 0)

    # ---------- 상태 조회 ----------

    def test_get_analysis_status_reports_retry_remaining(self):
        material = self._make_material()
        material.analysis_status = MaterialStatus.FAILED
        material.analysis_retry_count = 1
        material.analysis_error_message = "네트워크 오류"
        material.save(update_fields=["analysis_status", "analysis_retry_count", "analysis_error_message"])

        result = get_analysis_status(material)

        self.assertEqual(result["status"], MaterialStatus.FAILED)
        self.assertEqual(result["error_message"], "네트워크 오류")
        self.assertEqual(result["retry_count"], 1)
        self.assertEqual(result["retry_remaining"], MAX_RETRY_COUNT - 1)

    # ---------- 추출 시작과 분석 시작의 경쟁 상태 (리뷰 반영) ----------

    def test_initial_analysis_blocked_when_extraction_wins_race_after_status_check(self):
        """
        View가 material.status==COMPLETED를 확인한 시점(이 material 객체는 메모리에
        COMPLETED로 남아있음) 직후, DB에서는 PDF 재추출이 먼저 status=PROCESSING을
        차지했다고 가정한다. _start_processing()은 인메모리 값이 아니라 DB를 다시
        조건부로 확인하므로, 이 경우 분석 시작 자체가 원자적으로 실패해야 한다.
        """
        material = self._make_material()
        self.assertEqual(material.status, MaterialStatus.COMPLETED)  # View가 이미 확인한 상태

        # PDF 재추출이 먼저 DB에서 PROCESSING을 차지했다고 가정
        # (material 인메모리 객체는 건드리지 않아 "직후" 시점을 그대로 재현)
        StudyMaterial.objects.filter(pk=material.pk).update(status=MaterialStatus.PROCESSING)

        with self.assertRaises(DuplicateAnalysisRequestError):
            analyze_and_estimate(material)

        material.refresh_from_db()
        self.assertEqual(material.analysis_status, MaterialStatus.PENDING)  # 시작조차 안 됨
        self.assertEqual(material.status, MaterialStatus.PROCESSING)  # 추출 상태는 그대로

    def test_retry_blocked_when_extraction_wins_race_after_status_check(self):
        """재시도 경로에서도 동일한 경쟁 상태가 원자적으로 막혀야 한다."""
        material = self._make_material()
        material.analysis_status = MaterialStatus.FAILED
        material.analysis_retry_count = 0
        material.save(update_fields=["analysis_status", "analysis_retry_count"])

        StudyMaterial.objects.filter(pk=material.pk).update(status=MaterialStatus.PROCESSING)

        with self.assertRaises(DuplicateAnalysisRequestError):
            retry_analysis(material)

        material.refresh_from_db()
        self.assertEqual(material.analysis_status, MaterialStatus.FAILED)
        self.assertEqual(material.analysis_retry_count, 0)  # 증가 안 함
        self.assertEqual(material.status, MaterialStatus.PROCESSING)


class MaterialAnalysisViewTestCase(TestCase):
    """
    AI 분석 관련 View(material_analyze/material_retry_analyze/material_analysis_status) 검증.
    """

    def setUp(self):
        self.owner = User.objects.create_user(
            username="view_owner@example.com", email="view_owner@example.com", password="pass1234!"
        )
        self.other = User.objects.create_user(
            username="view_other@example.com", email="view_other@example.com", password="pass1234!"
        )
        self.period = ExamPeriod.objects.create(
            user=self.owner, title="뷰 테스트 시험기간",
            start_date=datetime.date(2026, 8, 1), end_date=datetime.date(2026, 8, 20),
        )
        self.exam = Exam.objects.create(
            exam_period=self.period, subject_name="뷰 테스트 과목", exam_date=datetime.date(2026, 8, 18),
        )
        self.material = StudyMaterial.objects.create(
            exam=self.exam, title="테스트 자료", material_type=MaterialType.TEXT,
            extracted_text="1장 개념 정리", status=MaterialStatus.COMPLETED,
        )

    def test_analyze_requires_login(self):
        response = self.client.post(reverse('exams:material_analyze', args=[self.material.id]))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response.url)

    def test_other_user_cannot_trigger_analyze(self):
        self.client.force_login(self.other)
        response = self.client.post(reverse('exams:material_analyze', args=[self.material.id]))
        self.assertEqual(response.status_code, 404)

    def test_analyze_rejected_when_extraction_not_completed(self):
        self.material.status = MaterialStatus.PENDING
        self.material.save(update_fields=["status"])
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse('exams:material_analyze', args=[self.material.id]), follow=True
        )

        self.material.refresh_from_db()
        self.assertEqual(self.material.analysis_status, MaterialStatus.PENDING)
        messages_list = list(response.context['messages'])
        self.assertTrue(any("텍스트 추출이 완료된 자료만" in str(m) for m in messages_list))

    def test_analyze_success_redirects_to_task_review(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse('exams:material_analyze', args=[self.material.id])
        )

        self.assertRedirects(response, reverse('exams:task_review', args=[self.exam.id]))
        self.material.refresh_from_db()
        self.assertEqual(self.material.analysis_status, MaterialStatus.COMPLETED)
        self.assertTrue(StudyTask.objects.filter(study_material=self.material).exists())

    def test_analyze_duplicate_request_shows_info_message(self):
        self.material.analysis_status = MaterialStatus.PROCESSING
        self.material.save(update_fields=["analysis_status"])
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse('exams:material_analyze', args=[self.material.id]), follow=True
        )

        messages_list = list(response.context['messages'])
        self.assertTrue(any("이미 분석 중" in str(m) for m in messages_list))

    def test_retry_from_failed_succeeds(self):
        self.material.analysis_status = MaterialStatus.FAILED
        self.material.analysis_retry_count = 0
        self.material.save(update_fields=["analysis_status", "analysis_retry_count"])
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse('exams:material_retry_analyze', args=[self.material.id])
        )

        self.assertRedirects(response, reverse('exams:task_review', args=[self.exam.id]))
        self.material.refresh_from_db()
        self.assertEqual(self.material.analysis_status, MaterialStatus.COMPLETED)
        self.assertEqual(self.material.analysis_retry_count, 1)

    def test_retry_blocked_when_completed(self):
        self.material.analysis_status = MaterialStatus.COMPLETED
        self.material.save(update_fields=["analysis_status"])
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse('exams:material_retry_analyze', args=[self.material.id]), follow=True
        )

        messages_list = list(response.context['messages'])
        self.assertTrue(any("재분석을 지원하지 않습니다" in str(m) for m in messages_list))

    def test_analysis_status_endpoint_returns_json(self):
        self.material.analysis_status = MaterialStatus.FAILED
        self.material.analysis_error_message = "네트워크 오류"
        self.material.analysis_retry_count = 1
        self.material.save(update_fields=["analysis_status", "analysis_error_message", "analysis_retry_count"])
        self.client.force_login(self.owner)

        response = self.client.get(reverse('exams:material_analysis_status', args=[self.material.id]))

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["analysis_status"], MaterialStatus.FAILED)
        self.assertEqual(data["analysis_error_message"], "네트워크 오류")
        self.assertEqual(data["retry_count"], 1)
        self.assertEqual(data["retry_remaining"], 1)

    def test_other_user_cannot_view_analysis_status(self):
        self.client.force_login(self.other)
        response = self.client.get(reverse('exams:material_analysis_status', args=[self.material.id]))
        self.assertEqual(response.status_code, 404)

    # ---------- stage/failed_stage 조합 (추출 상태 + 분석 상태 통합) ----------

    def _get_stage(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse('exams:material_analysis_status', args=[self.material.id]))
        return response.json()

    def test_stage_pending_when_nothing_started(self):
        self.material.status = MaterialStatus.PENDING
        self.material.analysis_status = MaterialStatus.PENDING
        self.material.save(update_fields=["status", "analysis_status"])

        data = self._get_stage()

        self.assertEqual(data["stage"], "PENDING")
        self.assertIsNone(data["failed_stage"])

    def test_stage_extracting_when_extraction_processing(self):
        self.material.status = MaterialStatus.PROCESSING
        self.material.save(update_fields=["status"])

        data = self._get_stage()

        self.assertEqual(data["stage"], "EXTRACTING")
        self.assertIsNone(data["failed_stage"])

    def test_stage_failed_with_extraction_when_extraction_failed(self):
        self.material.status = MaterialStatus.FAILED
        self.material.save(update_fields=["status"])

        data = self._get_stage()

        self.assertEqual(data["stage"], "FAILED")
        self.assertEqual(data["failed_stage"], "EXTRACTION")

    def test_stage_analyzing_when_analysis_processing(self):
        self.material.status = MaterialStatus.COMPLETED
        self.material.analysis_status = MaterialStatus.PROCESSING
        self.material.save(update_fields=["status", "analysis_status"])

        data = self._get_stage()

        self.assertEqual(data["stage"], "ANALYZING")
        self.assertIsNone(data["failed_stage"])

    def test_stage_failed_with_analysis_when_analysis_failed(self):
        self.material.status = MaterialStatus.COMPLETED
        self.material.analysis_status = MaterialStatus.FAILED
        self.material.save(update_fields=["status", "analysis_status"])

        data = self._get_stage()

        self.assertEqual(data["stage"], "FAILED")
        self.assertEqual(data["failed_stage"], "ANALYSIS")

    def test_stage_completed_when_analysis_completed(self):
        self.material.status = MaterialStatus.COMPLETED
        self.material.analysis_status = MaterialStatus.COMPLETED
        self.material.save(update_fields=["status", "analysis_status"])

        data = self._get_stage()

        self.assertEqual(data["stage"], "COMPLETED")
        self.assertIsNone(data["failed_stage"])

    def test_stage_prioritizes_extraction_over_stale_analysis_failure(self):
        """
        재추출 중(status=PROCESSING)인데 이전 분석 실패 기록(analysis_status=FAILED)이
        같이 남아있는 경우, 추출 상태를 우선해서 EXTRACTING으로 보여줘야 한다
        (이전 분석 실패가 잘못 노출되면 안 됨).
        """
        self.material.status = MaterialStatus.PROCESSING
        self.material.analysis_status = MaterialStatus.FAILED
        self.material.save(update_fields=["status", "analysis_status"])

        data = self._get_stage()

        self.assertEqual(data["stage"], "EXTRACTING")
        self.assertIsNone(data["failed_stage"])

    def test_stage_response_still_includes_existing_fields(self):
        """stage/failed_stage 추가가 retry_count/retry_remaining, 그리고
        analysis_status/analysis_error_message 값을 안 건드리는지 확인."""
        self.material.analysis_status = MaterialStatus.FAILED
        self.material.analysis_error_message = "테스트 실패 사유"
        self.material.analysis_retry_count = 1
        self.material.save(update_fields=[
            "analysis_status", "analysis_error_message", "analysis_retry_count",
        ])

        data = self._get_stage()

        self.assertEqual(data["analysis_status"], MaterialStatus.FAILED)
        self.assertEqual(data["analysis_error_message"], "테스트 실패 사유")
        self.assertEqual(data["retry_count"], 1)
        self.assertEqual(data["retry_remaining"], MAX_RETRY_COUNT - 1)

    def test_stage_response_includes_extraction_fields(self):
        """extraction_status/extraction_error_message가 material.status/
        error_message 값을 그대로 반영하는지 확인 (BE2와 합의한 필드명)."""
        self.material.status = MaterialStatus.FAILED
        self.material.error_message = "PDF 추출 실패 사유"
        self.material.save(update_fields=["status", "error_message"])

        data = self._get_stage()

        self.assertEqual(data["extraction_status"], MaterialStatus.FAILED)
        self.assertEqual(data["extraction_error_message"], "PDF 추출 실패 사유")

    # ---------- 예기치 못한 파이프라인 예외 처리 (PR #33 리뷰 반영) ----------

    @patch("exams.services.analysis_orchestrator.estimate_task_minutes")
    def test_analyze_unexpected_exception_redirects_instead_of_500(self, mock_estimate):
        mock_estimate.side_effect = ValueError("예상시간 계산 중 알 수 없는 오류")
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse('exams:material_analyze', args=[self.material.id]), follow=True
        )

        # 500이 아니라 자료 상세 화면으로 정상 리다이렉트되어야 한다
        self.assertEqual(response.status_code, 200)
        self.assertRedirects(
            response, reverse('exams:material_detail', args=[self.material.id])
        )
        messages_list = list(response.context['messages'])
        self.assertTrue(any("AI 분석에 실패했습니다" in str(m) for m in messages_list))

        self.material.refresh_from_db()
        self.assertEqual(self.material.analysis_status, MaterialStatus.FAILED)
        self.assertEqual(
            self.material.analysis_error_message,
            "분석 중 알 수 없는 오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
        )

    @patch("exams.services.analysis_orchestrator.estimate_task_minutes")
    def test_retry_unexpected_exception_redirects_instead_of_500(self, mock_estimate):
        mock_estimate.side_effect = ValueError("예상시간 계산 중 알 수 없는 오류")
        self.material.analysis_status = MaterialStatus.FAILED
        self.material.analysis_retry_count = 0
        self.material.save(update_fields=["analysis_status", "analysis_retry_count"])
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse('exams:material_retry_analyze', args=[self.material.id]), follow=True
        )

        self.assertEqual(response.status_code, 200)
        self.assertRedirects(
            response, reverse('exams:material_detail', args=[self.material.id])
        )
        messages_list = list(response.context['messages'])
        self.assertTrue(any("재시도한 AI 분석도 실패했습니다" in str(m) for m in messages_list))

        self.material.refresh_from_db()
        self.assertEqual(self.material.analysis_status, MaterialStatus.FAILED)
        self.assertEqual(
            self.material.analysis_error_message,
            "분석 중 알 수 없는 오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
        )
        # 재시도 자체는 시작됐으므로 retry_count는 증가한 상태로 남아야 한다
        self.assertEqual(self.material.analysis_retry_count, 1)


class PdfExtractorTestCase(TestCase):

    def test_extract_text_success(self):
        """[3번] 정상적인 텍스트 PDF에서 텍스트가 올바르게 추출되는지 검증"""
        # pypdf를 사용하여 텍스트가 포함된 PDF 메모리 상에 동적 생성
        # 1페이지짜리 샘플 PDF 세팅 (텍스트 포함)
        # Note: pypdf로 텍스트 오브젝트 직접 주입이 안 될 수 있어 표준 Stream 방식을 사용하거나
        # ReportLab 등이 없는 환경을 고려한 기본 텍스트 포함 1페이지 생성
        raw_pdf_data = b"""%PDF-1.4
1 0 obj <</Type /Catalog /Pages 2 0 R>> endobj
2 0 obj <</Type /Pages /Kids [3 0 R] /Count 1>> endobj
3 0 obj <</Type /Page /Parent 2 0 R /Resources <</Font <</F1 4 0 R>>>> /MediaBox [0 0 612 792] /Contents 5 0 R>> endobj
4 0 obj <</Type /Font /Subtype /Type1 /BaseFont /Helvetica>> endobj
5 0 obj <</Length 55>> stream
BT
/F1 12 Tf
100 700 Td
(Hello Plan B PDF Text Extraction) Tj
ET
endstream endobj
xref
0 6
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000115 00000 n 
0000000231 00000 n 
0000000300 00000 n 
trailer <</Size 6 /Root 1 0 R>>
startxref
406
%%EOF"""

        dummy_file = SimpleUploadedFile("valid_sample.pdf", raw_pdf_data, content_type="application/pdf")

        # 텍스트 추출 실행
        extracted_text = extract_text_from_pdf(dummy_file)

        # 검증
        self.assertIn("Hello Plan B PDF Text Extraction", extracted_text)

    def test_extract_text_encrypted_with_empty_password(self):
        """[4번] 빈 비밀번호("")로 해제 가능한 암호화 PDF 처리 검증"""
        writer = pypdf.PdfWriter()
        page = writer.add_blank_page(width=100, height=100)
        
        # 빈 비밀번호("")로 읽기 암호화 설정
        writer.encrypt(user_password="", owner_password="")

        pdf_buffer = io.BytesIO()
        writer.write(pdf_buffer)
        pdf_buffer.seek(0)

        dummy_file = SimpleUploadedFile("encrypted_empty_pass.pdf", pdf_buffer.read(), content_type="application/pdf")

        # 빈 비밀번호 해제 시도 후 텍스트 추출 동작 시도 (내용이 없으므로 빈 PDF 예외 혹은 정상 통과 확인)
        # 빈 페이지이므로 PdfExtractionError("PDF에서 텍스트를 추출할 수 없습니다...")가 발생해야 decrypt("") 단계를 무사히 통과한 것임
        with self.assertRaises(PdfExtractionError) as context:
            extract_text_from_pdf(dummy_file)

        # "암호화된 PDF 파일은 지원하지 않습니다"가 아닌, decrypt 통과 후 "텍스트를 추출할 수 없습니다" 메시지가 나와야 성공!
        self.assertIn("PDF에서 텍스트를 추출할 수 없습니다", str(context.exception))

    def test_extract_text_from_invalid_pdf(self):
        """손상되었거나 일반 텍스트 파일 입력 시 예외 검증"""
        dummy_file = SimpleUploadedFile("invalid.pdf", b"Not a PDF content", content_type="application/pdf")

        with self.assertRaises(PdfExtractionError) as context:
            extract_text_from_pdf(dummy_file)

        self.assertIn("올바른 PDF 형식이 아니거나 손상된 파일입니다", str(context.exception))

    def test_extract_text_from_empty_pdf_or_image(self):
        """텍스트 레이어가 없는 빈/스캔 PDF일 때 예외 처리 검증"""
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=100, height=100)

        pdf_buffer = io.BytesIO()
        writer.write(pdf_buffer)
        pdf_buffer.seek(0)

        dummy_file = SimpleUploadedFile("blank.pdf", pdf_buffer.read(), content_type="application/pdf")

        with self.assertRaises(PdfExtractionError) as context:
            extract_text_from_pdf(dummy_file)

        self.assertIn("PDF에서 텍스트를 추출할 수 없습니다", str(context.exception))

class AITransactionIsolationTestCase(TransactionTestCase):
    """
    이슈: task_extractor.analyze_study_material()가 통째로 @transaction.atomic이라,
    그 안에서 벌어지는 AI 네트워크 호출이 DB 트랜잭션을 물고 있는 채로 실행되던 문제.

    fetch_extracted_tasks()(네트워크, 트랜잭션 없음)와 save_extracted_tasks()/
    _save_tasks_with_estimates()(DB 쓰기, 짧은 트랜잭션)로 분리한 뒤,
    AI 호출 시점에 실제로 열려있는 DB 트랜잭션이 없는지 직접 검증한다.

    TestCase가 아니라 TransactionTestCase를 쓰는 이유: 일반 TestCase는 테스트
    하나하나를 자체적으로 큰 트랜잭션으로 감싸서 롤백하기 때문에, 그 안에서는
    connection.in_atomic_block이 항상 True로 나와 이 검증 자체가 무의미해진다.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="tx_tester@example.com", email="tx_tester@example.com", password="pass1234!"
        )
        self.period = ExamPeriod.objects.create(
            user=self.user, title="트랜잭션 격리 테스트",
            start_date=datetime.date(2026, 8, 1), end_date=datetime.date(2026, 8, 20),
        )
        self.exam = Exam.objects.create(
            exam_period=self.period, subject_name="테스트과목", exam_date=datetime.date(2026, 8, 18),
        )
        self.material = StudyMaterial.objects.create(
            exam=self.exam, title="테스트 자료", extracted_text="1장 개념 정리",
            status=MaterialStatus.COMPLETED,
        )

    @override_settings(AI_MOCK_MODE=True)
    def test_call_ai_runs_without_open_transaction(self):
        observed_in_atomic_block = []

        def spy_call_ai(prompt):
            # 이 테스트의 목적은 트랜잭션 유무 확인이지 실제 AI 응답 확인이 아니므로,
            # 테스트 실행 환경에서 AI_MOCK_MODE가 어쩌다 False로 덮어써져도 실제
            # Gemini API를 호출하지 않도록 고정 응답을 직접 반환한다.
            observed_in_atomic_block.append(connection.in_atomic_block)
            return task_extractor._MOCK_RESPONSE

        with patch("exams.services.task_extractor._call_ai", side_effect=spy_call_ai):
            analyze_and_estimate(self.material)

        self.assertEqual(len(observed_in_atomic_block), 1)
        self.assertFalse(
            observed_in_atomic_block[0],
            "AI 네트워크 호출(_call_ai) 시점에 DB 트랜잭션이 열려있으면 안 된다.",
        )

    @override_settings(AI_MOCK_MODE=True)
    def test_studytask_creation_still_rolls_back_on_db_failure(self):
        """
        네트워크 호출을 트랜잭션 밖으로 뺐어도, DB 저장 단계 자체의 원자성은
        여전히 보장되어야 한다 (StudyTask 생성 + 예상시간 반영이 한 단위로 롤백).
        """
        with patch("exams.services.analysis_orchestrator.estimate_task_minutes") as mock_estimate:
            mock_estimate.side_effect = ValueError("예상시간 계산 중 알 수 없는 오류")
            with self.assertRaises(AnalysisPipelineError):
                analyze_and_estimate(self.material)

        self.assertEqual(StudyTask.objects.filter(study_material=self.material).count(), 0)

    @override_settings(AI_MOCK_MODE=True)
    def test_stale_extracted_text_discards_result(self):
        """
        AI 호출 시작 이후 저장 시점 사이에 extracted_text가 바뀌면(예: 다른 요청이
        PDF를 재추출), 그 사이 받은 AI 결과는 저장하지 않고 버려야 한다.
        """
        def fake_fetch(exam, extracted_text):
            # AI 응답을 기다리는 동안 다른 요청이 텍스트를 바꿔치기했다고 가정
            StudyMaterial.objects.filter(pk=self.material.pk).update(
                extracted_text="다른 요청이 재추출한 새 텍스트"
            )
            return task_extractor.fetch_extracted_tasks(exam, extracted_text)

        with patch(
            "exams.services.analysis_orchestrator.fetch_extracted_tasks", side_effect=fake_fetch
        ):
            with self.assertRaises(AnalysisPipelineError):
                analyze_and_estimate(self.material)

        self.assertEqual(StudyTask.objects.filter(study_material=self.material).count(), 0)
        self.material.refresh_from_db()
        self.assertEqual(self.material.analysis_status, MaterialStatus.FAILED)
        # 텍스트 자체는 다른 요청이 바꾼 값 그대로 남아있어야 한다 (이 실행이 덮어쓰면 안 됨)
        self.assertEqual(self.material.extracted_text, "다른 요청이 재추출한 새 텍스트")

    @override_settings(AI_MOCK_MODE=True)
    def test_stale_when_extraction_reprocessing_even_if_text_unchanged(self):
        """
        PDF 재추출이 "시작"만 되고(status=PROCESSING) 아직 extracted_text 자체는
        안 바뀐 시점에도, 저장을 포기해야 한다 (텍스트 비교만으로는 이 시점을
        걸러낼 수 없어서 status도 별도로 확인해야 하는 케이스).
        """
        original_text = self.material.extracted_text

        def fake_fetch(exam, extracted_text):
            # 재추출이 막 시작됐다고 가정: status만 PROCESSING으로 바뀌고
            # extracted_text는 아직 원래 값 그대로인 상태
            StudyMaterial.objects.filter(pk=self.material.pk).update(
                status=MaterialStatus.PROCESSING
            )
            return task_extractor.fetch_extracted_tasks(exam, extracted_text)

        with patch(
            "exams.services.analysis_orchestrator.fetch_extracted_tasks", side_effect=fake_fetch
        ):
            with self.assertRaises(AnalysisPipelineError):
                analyze_and_estimate(self.material)

        self.assertEqual(StudyTask.objects.filter(study_material=self.material).count(), 0)
        self.material.refresh_from_db()
        self.assertEqual(self.material.analysis_status, MaterialStatus.FAILED)
        # 텍스트 자체는 그대로였다는 것도 재확인 (이게 이 테스트의 핵심 포인트)
        self.assertEqual(self.material.extracted_text, original_text)

    @override_settings(AI_MOCK_MODE=True)
    def test_speed_factor_uses_latest_value_at_save_time(self):
        """
        예상시간 계산은 AI 호출 전에 로드해둔 오래된 exam 객체가 아니라, 저장
        시점에 다시 조회한 최신 speed_factor를 사용해야 한다.
        """
        def fake_fetch(exam, extracted_text):
            # AI 응답을 기다리는 동안 progress 기록으로 speed_factor가 갱신됐다고 가정
            Exam.objects.filter(pk=self.exam.pk).update(speed_factor=2.0)
            return task_extractor.fetch_extracted_tasks(exam, extracted_text)

        with patch(
            "exams.services.analysis_orchestrator.fetch_extracted_tasks", side_effect=fake_fetch
        ):
            tasks = analyze_and_estimate(self.material)

        from planner.services.time_estimator import estimate_task_minutes

        first_task = tasks[0]
        expected_min, expected_max = estimate_task_minutes(
            task_type=first_task.task_type,
            difficulty=first_task.difficulty,
            speed_factor=2.0,  # 저장 시점의 최신값
        )
        self.assertEqual(first_task.estimated_min_minutes, expected_min)
        self.assertEqual(first_task.estimated_max_minutes, expected_max)

    @override_settings(AI_MOCK_MODE=True)
    def test_existing_unconfirmed_task_preserved_when_save_rolls_back(self):
        """
        save_extracted_tasks()는 기존 미확정 작업을 삭제하고 새로 만드는데, 그
        직후(예상시간 계산) 실패로 트랜잭션이 롤백되면 기존 작업 삭제도 함께
        되돌려져서 그대로 남아있어야 한다.
        """
        old_task = StudyTask.objects.create(
            exam=self.exam, study_material=self.material, title="기존 작업",
            task_type="concept", importance="medium", depth="basic",
            difficulty="normal", estimated_min_minutes=10, estimated_max_minutes=20,
            order=1,
        )

        with patch("exams.services.analysis_orchestrator.estimate_task_minutes") as mock_estimate:
            mock_estimate.side_effect = ValueError("예상시간 계산 중 알 수 없는 오류")
            with self.assertRaises(AnalysisPipelineError):
                analyze_and_estimate(self.material)

        self.assertTrue(
            StudyTask.objects.filter(pk=old_task.pk, title="기존 작업").exists()
        )