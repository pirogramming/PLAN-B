import datetime
import io
import pypdf
from unittest.mock import patch

from django.test import TestCase, Client
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
    PriorityLevel,
    TaskDepth,
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


class TaskReviewFormSubmitTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username="testuser", password="password123"
        )
        self.client.force_login(self.user)

        self.period = ExamPeriod.objects.create(
            user=self.user,
            title="2026 2학기 중간고사",
            start_date="2026-08-01",
            end_date="2026-08-15",
        )
        self.exam = Exam.objects.create(
            exam_period=self.period,
            subject_name="자료구조",
            exam_date="2026-08-10",
            speed_factor=1.0,
        )
        self.task = StudyTask.objects.create(
            exam=self.exam,
            title="기존 제목",
            task_type=TaskType.CONCEPT,
            importance=PriorityLevel.MEDIUM,
            depth=TaskDepth.BASIC,
            difficulty=TaskDifficulty.NORMAL,
            is_confirmed=False,
        )
        self.url = reverse("exams:task_review", kwargs={"exam_id": self.exam.id})

    def test_save_only_action(self):
        """'수정사항 저장' 버튼(action=save) 클릭 시 DB 수정 후 리뷰 페이지로 리다이렉트 검증"""
        post_data = {
            "form-TOTAL_FORMS": "1",
            "form-INITIAL_FORMS": "1",
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
            "form-0-id": self.task.id,
            "form-0-unit_name": "1장 개념",
            "form-0-title": "수정된 제목 (저장만)",
            "form-0-task_type": TaskType.CONCEPT,
            "form-0-importance": PriorityLevel.MEDIUM,
            "form-0-depth": TaskDepth.BASIC,
            "form-0-difficulty": TaskDifficulty.HARD,
            "action": "save",
        }

        response = self.client.post(self.url, post_data)
        self.assertRedirects(response, self.url)

        self.task.refresh_from_db()
        self.assertEqual(self.task.title, "수정된 제목 (저장만)")
        self.assertEqual(self.task.difficulty, TaskDifficulty.HARD)
        self.assertFalse(self.task.is_confirmed)

    @patch("exams.views.redirect")
    def test_confirm_and_next_action(self, mock_redirect):
        """'모두 확정하고 다음으로' 버튼 클릭 시 views.py 수정 없이 redirect mock으로 URL 미연결 처리"""
        from django.http import HttpResponseRedirect
        expected_redirect_url = f"/planner/period/{self.period.id}/feasibility/"
        mock_redirect.return_value = HttpResponseRedirect(expected_redirect_url)

        post_data = {
            "form-TOTAL_FORMS": "1",
            "form-INITIAL_FORMS": "1",
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
            "form-0-id": self.task.id,
            "form-0-unit_name": "1장 개념",
            "form-0-title": "수정된 제목 (저장+확정)",
            "form-0-task_type": TaskType.CONCEPT,
            "form-0-importance": PriorityLevel.HIGH,
            "form-0-depth": TaskDepth.CORE,
            "form-0-difficulty": TaskDifficulty.EASY,
            "action": "confirm_and_next",
        }

        response = self.client.post(self.url, post_data)

        mock_redirect.assert_called_once_with(
            'planner:feasibility', period_id=self.period.id
        )

        self.task.refresh_from_db()
        self.assertEqual(self.task.title, "수정된 제목 (저장+확정)")
        self.assertTrue(self.task.is_confirmed)


class MaterialAnalysisStatusViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username="testuser2", password="password123"
        )
        self.client.force_login(self.user)

        self.period = ExamPeriod.objects.create(
            user=self.user,
            title="시험기간",
            start_date="2026-08-01",
            end_date="2026-08-15",
        )
        self.exam = Exam.objects.create(
            exam_period=self.period,
            subject_name="운영체제",
            exam_date="2026-08-10",
        )
        self.material = StudyMaterial.objects.create(
            exam=self.exam,
            title="운영체제 Ch1",
            status=MaterialStatus.COMPLETED,
            analysis_status=MaterialStatus.PROCESSING,
        )
        self.url = reverse(
            "exams:material_analysis_status",
            kwargs={"material_id": self.material.id},
        )

    def test_analysis_status_endpoint_returns_json(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

        data = response.json()
        self.assertEqual(data["stage"], "ANALYZING")
        self.assertEqual(data["extraction_status"], MaterialStatus.COMPLETED)
        self.assertEqual(data["analysis_status"], MaterialStatus.PROCESSING)
        self.assertEqual(data["study_material_id"], self.material.id)
        self.assertEqual(data["exam_id"], self.exam.id)


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
        self.assertEqual(material.analysis_status, MaterialStatus.PENDING)

    def test_pdf_material_status_stays_pending_until_extracted(self):
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
            'form-0-difficulty': TaskDifficulty.HARD,
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
        )

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
        self.assertEqual(material.status, MaterialStatus.COMPLETED)
        self.assertIsNone(material.error_message)

    @patch("exams.services.analysis_orchestrator.analyze_study_material")
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

        with self.assertRaises(AnalysisPipelineError):
            analyze_and_estimate(material)

        material.refresh_from_db()
        self.assertEqual(material.analysis_status, MaterialStatus.FAILED)
        self.assertNotIn("예상시간 계산 중 알 수 없는 오류", material.analysis_error_message or "")
        self.assertEqual(StudyTask.objects.filter(study_material=material).count(), 0)

    @patch("exams.services.analysis_orchestrator.analyze_study_material")
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

        with patch("exams.services.analysis_orchestrator.analyze_study_material") as mock_analyze:
            mock_analyze.side_effect = AICallFailedError("1차 재시도 실패")
            with self.assertRaises(AICallFailedError):
                retry_analysis(material)

        material.refresh_from_db()
        self.assertEqual(material.analysis_retry_count, 1)
        self.assertEqual(material.analysis_status, MaterialStatus.FAILED)

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
        mock_call_ai.side_effect = [
            "이건 유효하지 않은 JSON 입니다",
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


class MaterialAnalysisViewTestCase(TestCase):
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
        self.assertEqual(data["extraction_status"], MaterialStatus.COMPLETED)
        self.assertEqual(data["study_material_id"], self.material.id)
        self.assertEqual(data["exam_id"], self.exam.id)

    def test_other_user_cannot_view_analysis_status(self):
        self.client.force_login(self.other)
        response = self.client.get(reverse('exams:material_analysis_status', args=[self.material.id]))
        self.assertEqual(response.status_code, 404)

    @patch("exams.services.analysis_orchestrator.estimate_task_minutes")
    def test_analyze_unexpected_exception_redirects_instead_of_500(self, mock_estimate):
        mock_estimate.side_effect = ValueError("예상시간 계산 중 알 수 없는 오류")
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse('exams:material_analyze', args=[self.material.id]), follow=True
        )

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
        self.assertEqual(self.material.analysis_retry_count, 1)


class PdfExtractorTestCase(TestCase):

    def test_extract_text_success(self):
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

        extracted_text = extract_text_from_pdf(dummy_file)

        self.assertIn("Hello Plan B PDF Text Extraction", extracted_text)

    def test_extract_text_encrypted_with_empty_password(self):
        writer = pypdf.PdfWriter()
        page = writer.add_blank_page(width=100, height=100)
        
        writer.encrypt(user_password="", owner_password="")

        pdf_buffer = io.BytesIO()
        writer.write(pdf_buffer)
        pdf_buffer.seek(0)

        dummy_file = SimpleUploadedFile("encrypted_empty_pass.pdf", pdf_buffer.read(), content_type="application/pdf")

        with self.assertRaises(PdfExtractionError) as context:
            extract_text_from_pdf(dummy_file)

        self.assertIn("PDF에서 텍스트를 추출할 수 없습니다", str(context.exception))

    def test_extract_text_from_invalid_pdf(self):
        dummy_file = SimpleUploadedFile("invalid.pdf", b"Not a PDF content", content_type="application/pdf")

        with self.assertRaises(PdfExtractionError) as context:
            extract_text_from_pdf(dummy_file)

        self.assertIn("올바른 PDF 형식이 아니거나 손상된 파일입니다", str(context.exception))

    def test_extract_text_from_empty_pdf_or_image(self):
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=100, height=100)

        pdf_buffer = io.BytesIO()
        writer.write(pdf_buffer)
        pdf_buffer.seek(0)

        dummy_file = SimpleUploadedFile("blank.pdf", pdf_buffer.read(), content_type="application/pdf")

        with self.assertRaises(PdfExtractionError) as context:
            extract_text_from_pdf(dummy_file)

        self.assertIn("PDF에서 텍스트를 추출할 수 없습니다", str(context.exception))