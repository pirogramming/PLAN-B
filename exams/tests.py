import datetime
import io
import json
import logging
import uuid
import shutil
import tempfile
from unittest.mock import patch

import pypdfium2 as pdfium
from django.db import transaction
from django.conf import settings
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings, Client
from django.urls import reverse
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import ProtectedError

from .models import ExamPeriod, Exam, AvailableTime, StudyMaterial, StudyTask
from core.choices import (
    ExamPeriodStatus,
    MaterialStatus,
    MaterialType,
    TaskType,
    TaskDifficulty,
    PriorityLevel,
    TaskDepth,
    RecoveryActionType,
    RecoveryPlanStatus,
    RecoveryType,
    DailyPlanStatus
)
from core.exceptions import AICallFailedError, AIResponseValidationError
from exams.services.analysis_orchestrator import (
    AnalysisNotSupportedError,
    AnalysisPipelineError,
    DuplicateAnalysisRequestError,
    MAX_RETRY_COUNT,
    PROCESSING_TIMEOUT_SECONDS,
    RetryLimitExceededError,
    StaleAnalysisRunError,
    _execute_analysis,
    _finish_failure,
    _save_tasks_with_estimates,
    _start_processing,
    analyze_and_estimate,
    get_analysis_status,
    retry_analysis,
)

from exams.services import task_extractor
from exams.services.pdf_extractor import extract_text_from_pdf, PdfExtractionError
from planner.models import DailyPlan, DailyPlanItem, RecoveryPlan, RecoveryPlanItem
User = get_user_model()
logger = logging.getLogger(__name__)
TEMP_MEDIA_ROOT = tempfile.mkdtemp()


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

    def test_confirm_and_next_action(self):
        """'모두 확정하고 다음으로' 버튼 클릭 시 실제 planner:feasibility 라우팅 검증"""
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

        self.assertRedirects(
            response,
            reverse(
                "planner:feasibility",
                kwargs={"period_id": self.period.id},
            ),
        )

        self.task.refresh_from_db()
        self.assertEqual(self.task.title, "수정된 제목 (저장+확정)")
        self.assertTrue(self.task.is_confirmed)


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
        mock_extract.side_effect = PdfExtractionError("PDF에서 텍스트를 추출할 수 없습니다.")
        material = self._make_pdf_material()

        response = self.client.post(reverse('exams:material_extract', args=[material.id]))
        material.refresh_from_db()

        self.assertEqual(material.status, MaterialStatus.FAILED)
        self.assertIn("추출할 수 없습니다", material.error_message)

    @patch('exams.views.extract_text_from_pdf')
    def test_extract_success_resets_stale_analysis_state(self, mock_extract):
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

        with self.assertRaises(AnalysisPipelineError):
            analyze_and_estimate(material)

        material.refresh_from_db()
        self.assertEqual(material.analysis_status, MaterialStatus.FAILED)
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

        with patch("exams.services.analysis_orchestrator.fetch_extracted_tasks") as mock_analyze:
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

    def test_initial_analysis_blocked_when_extraction_wins_race_after_status_check(self):
        material = self._make_material()
        self.assertEqual(material.status, MaterialStatus.COMPLETED)

        StudyMaterial.objects.filter(pk=material.pk).update(status=MaterialStatus.PROCESSING)

        with self.assertRaises(DuplicateAnalysisRequestError):
            analyze_and_estimate(material)

        material.refresh_from_db()
        self.assertEqual(material.analysis_status, MaterialStatus.PENDING)
        self.assertEqual(material.status, MaterialStatus.PROCESSING)

    def test_retry_blocked_when_extraction_wins_race_after_status_check(self):
        material = self._make_material()
        material.analysis_status = MaterialStatus.FAILED
        material.analysis_retry_count = 0
        material.save(update_fields=["analysis_status", "analysis_retry_count"])

        StudyMaterial.objects.filter(pk=material.pk).update(status=MaterialStatus.PROCESSING)

        with self.assertRaises(DuplicateAnalysisRequestError):
            retry_analysis(material)

        material.refresh_from_db()
        self.assertEqual(material.analysis_status, MaterialStatus.FAILED)
        self.assertEqual(material.analysis_retry_count, 0)
        self.assertEqual(material.status, MaterialStatus.PROCESSING)


class PdfExtractorTestCase(TestCase):
    """pypdfium2 및 OCR 기반 텍스트 추출 파이프라인 검증"""

    def test_extract_text_success(self):
        """정상적인 텍스트 PDF에서 텍스트가 올바르게 추출되는지 검증"""
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

    def test_extract_text_encrypted_not_supported(self):
        """pypdf 기반으로 실제 비밀번호가 걸린 암호화 PDF를 생성하여
        pypdfium2가 암호화된 PDF 예외를 처리하는지 검증"""
        import pypdf
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=100, height=100)
        # 빈 비밀번호는 pypdfium2가 자동으로 빈 암호로 열어버릴 수 있어
        # "암호화 분기"가 아니라 "빈 페이지라 텍스트 없음" 분기로 우연히 통과할 위험이 있음.
        # 실제 사용자 비밀번호를 걸어서 진짜 암호화 예외 분기를 검증한다.
        writer.encrypt(user_password="secret_password", owner_password="secret_password")

        pdf_buffer = io.BytesIO()
        writer.write(pdf_buffer)
        pdf_buffer.seek(0)

        dummy_file = SimpleUploadedFile("encrypted.pdf", pdf_buffer.read(), content_type="application/pdf")

        with self.assertRaises(PdfExtractionError) as context:
            extract_text_from_pdf(dummy_file)

        self.assertIn("암호화된 PDF 파일은 지원하지 않습니다", str(context.exception))

    def test_extract_text_encrypted_with_empty_password(self):
        import pypdf
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.encrypt(user_password="", owner_password="")

        pdf_buffer = io.BytesIO()
        writer.write(pdf_buffer)
        pdf_buffer.seek(0)

        dummy_file = SimpleUploadedFile("encrypted.pdf", pdf_buffer.read(), content_type="application/pdf")

        with self.assertRaises(PdfExtractionError) as context:
            extract_text_from_pdf(dummy_file)

        self.assertIn("PDF에서 텍스트를 추출할 수 없습니다", str(context.exception))

    def test_extract_text_from_invalid_pdf(self):
        dummy_file = SimpleUploadedFile("invalid.pdf", b"Not a PDF content", content_type="application/pdf")

        with self.assertRaises(PdfExtractionError) as context:
            extract_text_from_pdf(dummy_file)

        self.assertIn("올바른 PDF 형식이 아니거나 손상된 파일입니다", str(context.exception))

    def test_extract_text_from_empty_pdf_or_image(self):
        import pypdf
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=100, height=100)

        pdf_buffer = io.BytesIO()
        writer.write(pdf_buffer)
        pdf_buffer.seek(0)

        dummy_file = SimpleUploadedFile("blank.pdf", pdf_buffer.read(), content_type="application/pdf")

        with patch("exams.services.pdf_extractor.OCR_AVAILABLE", False):
            with self.assertRaises(PdfExtractionError) as context:
                extract_text_from_pdf(dummy_file)

        self.assertIn("PDF에서 텍스트를 추출할 수 없습니다", str(context.exception))


class AITransactionIsolationTestCase(TransactionTestCase):
    """
    이슈: task_extractor.analyze_study_material()가 통째로 @transaction.atomic이라,
    그 안에서 벌어지는 AI 네트워크 호출이 DB 트랜잭션을 물고 있는 채로 실행되던 문제.
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
        with patch("exams.services.analysis_orchestrator.estimate_task_minutes") as mock_estimate:
            mock_estimate.side_effect = ValueError("예상시간 계산 중 알 수 없는 오류")
            with self.assertRaises(AnalysisPipelineError):
                analyze_and_estimate(self.material)

        self.assertEqual(StudyTask.objects.filter(study_material=self.material).count(), 0)

    @override_settings(AI_MOCK_MODE=True)
    def test_stale_extracted_text_discards_result(self):
        def fake_fetch(exam, extracted_text):
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
        self.assertEqual(self.material.extracted_text, "다른 요청이 재추출한 새 텍스트")

    @override_settings(AI_MOCK_MODE=True)
    def test_stale_when_extraction_reprocessing_even_if_text_unchanged(self):
        original_text = self.material.extracted_text

        def fake_fetch(exam, extracted_text):
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
        self.assertEqual(self.material.extracted_text, original_text)

    @override_settings(AI_MOCK_MODE=True)
    def test_speed_factor_uses_latest_value_at_save_time(self):
        def fake_fetch(exam, extracted_text):
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
            speed_factor=2.0,
        )
        self.assertEqual(first_task.estimated_min_minutes, expected_min)
        self.assertEqual(first_task.estimated_max_minutes, expected_max)

    @override_settings(AI_MOCK_MODE=True)
    def test_existing_unconfirmed_task_preserved_when_save_rolls_back(self):
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


class ProcessingTimeoutTestCase(TestCase):
    """
    이슈 #52: 서버가 AI 분석 도중 비정상 종료되면 analysis_status가 PROCESSING으로
    영원히 남아, 이후 어떤 분석/재시도 요청도 거부되는(좀비 상태) 문제 검증.

    - 좀비 구제는 retry_analysis()에서만 허용 (analyze_and_estimate()는 PENDING 전용)
    - 재시도 횟수 제한을 FAILED/좀비 PROCESSING 양쪽에 동일하게 적용
    - analysis_started_at이 NULL인 PROCESSING도 좀비로 취급
    - 실행 소유권(analysis_run_id)으로 늦게 끝난 예전 실행이 최신 실행 결과를
      덮어쓰지 못하게 방지
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="timeout_tester@example.com", email="timeout_tester@example.com", password="pass1234!"
        )
        self.period = ExamPeriod.objects.create(
            user=self.user, title="타임아웃 테스트",
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

    def _make_stale_processing(self, retry_count=0, started_at="stale"):
        material = self._make_material()
        material.analysis_status = MaterialStatus.PROCESSING
        material.analysis_retry_count = retry_count
        material.analysis_run_id = uuid.uuid4()
        if started_at == "stale":
            material.analysis_started_at = (
                timezone.now() - datetime.timedelta(seconds=PROCESSING_TIMEOUT_SECONDS + 1)
            )
        elif started_at is None:
            material.analysis_started_at = None
        else:
            material.analysis_started_at = started_at
        material.save(update_fields=[
            "analysis_status", "analysis_retry_count", "analysis_started_at", "analysis_run_id",
        ])
        return material

    def test_start_processing_records_started_at_and_run_id(self):
        material = self._make_material()
        before = timezone.now()

        analyze_and_estimate(material)

        material.refresh_from_db()
        self.assertIsNotNone(material.analysis_started_at)
        self.assertGreaterEqual(material.analysis_started_at, before)
        self.assertIsNotNone(material.analysis_run_id)

    def test_fresh_processing_still_blocks_duplicate_request(self):
        material = self._make_material()
        material.analysis_status = MaterialStatus.PROCESSING
        material.analysis_started_at = timezone.now()
        material.save(update_fields=["analysis_status", "analysis_started_at"])

        with self.assertRaises(DuplicateAnalysisRequestError):
            analyze_and_estimate(material)

        with self.assertRaises(DuplicateAnalysisRequestError):
            retry_analysis(material)

    def test_initial_analysis_does_not_rescue_zombie_processing(self):
        material = self._make_stale_processing(retry_count=0)

        with self.assertRaises(DuplicateAnalysisRequestError):
            analyze_and_estimate(material)

        material.refresh_from_db()
        self.assertEqual(material.analysis_status, MaterialStatus.PROCESSING)
        self.assertEqual(material.analysis_retry_count, 0)

    def test_retry_can_rescue_zombie_processing(self):
        material = self._make_stale_processing(retry_count=0)

        tasks = retry_analysis(material)

        material.refresh_from_db()
        self.assertTrue(len(tasks) > 0)
        self.assertEqual(material.analysis_status, MaterialStatus.COMPLETED)
        self.assertEqual(material.analysis_retry_count, 1)

    def test_retry_rejected_when_retry_count_maxed_even_if_zombie(self):
        material = self._make_stale_processing(retry_count=MAX_RETRY_COUNT)

        with self.assertRaises(RetryLimitExceededError):
            retry_analysis(material)

        material.refresh_from_db()
        self.assertEqual(material.analysis_retry_count, MAX_RETRY_COUNT)
        self.assertEqual(material.analysis_status, MaterialStatus.PROCESSING)

    def test_retry_race_loser_gets_duplicate_request_not_retry_limit_exceeded(self):
        material = self._make_material()
        material.analysis_status = MaterialStatus.FAILED
        material.analysis_retry_count = MAX_RETRY_COUNT - 1
        material.save(update_fields=["analysis_status", "analysis_retry_count"])

        StudyMaterial.objects.filter(pk=material.pk).update(
            analysis_status=MaterialStatus.PROCESSING,
            analysis_retry_count=MAX_RETRY_COUNT,
            analysis_started_at=timezone.now(),
            analysis_run_id=uuid.uuid4(),
        )
        material.refresh_from_db()

        with self.assertRaises(DuplicateAnalysisRequestError):
            retry_analysis(material)

    def test_retry_can_rescue_zombie_with_null_started_at(self):
        material = self._make_stale_processing(retry_count=0, started_at=None)

        tasks = retry_analysis(material)

        material.refresh_from_db()
        self.assertTrue(len(tasks) > 0)
        self.assertEqual(material.analysis_status, MaterialStatus.COMPLETED)
        self.assertEqual(material.analysis_retry_count, 1)

    def test_get_analysis_status_is_stale_true_when_started_at_null(self):
        material = self._make_stale_processing(retry_count=0, started_at=None)
        result = get_analysis_status(material)
        self.assertTrue(result["is_stale"])

    def test_get_analysis_status_is_stale_true_when_zombie(self):
        material = self._make_stale_processing(retry_count=0)
        result = get_analysis_status(material)
        self.assertTrue(result["is_stale"])

    def test_get_analysis_status_is_stale_false_when_fresh(self):
        material = self._make_material()
        material.analysis_status = MaterialStatus.PROCESSING
        material.analysis_started_at = timezone.now()
        material.save(update_fields=["analysis_status", "analysis_started_at"])

        result = get_analysis_status(material)
        self.assertFalse(result["is_stale"])

    def test_get_analysis_status_is_stale_false_when_not_processing(self):
        material = self._make_material()
        material.analysis_status = MaterialStatus.PENDING
        result = get_analysis_status(material)
        self.assertFalse(result["is_stale"])

    def test_can_retry_false_within_5min_processing(self):
        material = self._make_material()
        material.analysis_status = MaterialStatus.PROCESSING
        material.analysis_started_at = timezone.now() - datetime.timedelta(minutes=2)
        material.save(update_fields=["analysis_status", "analysis_started_at"])

        result = get_analysis_status(material)

        self.assertFalse(result["can_retry"])
        self.assertIsNotNone(result["retry_after_seconds"])
        self.assertTrue(170 <= result["retry_after_seconds"] <= 180)

    def test_can_retry_true_when_processing_over_5min(self):
        material = self._make_stale_processing(retry_count=0)
        result = get_analysis_status(material)

        self.assertTrue(result["can_retry"])
        self.assertIsNone(result["retry_after_seconds"])

    def test_can_retry_true_when_failed_with_retries_left(self):
        material = self._make_material()
        material.analysis_status = MaterialStatus.FAILED
        material.analysis_retry_count = 1
        material.save(update_fields=["analysis_status", "analysis_retry_count"])

        result = get_analysis_status(material)

        self.assertTrue(result["can_retry"])
        self.assertIsNone(result["retry_after_seconds"])

    def test_can_retry_false_when_retries_exhausted_even_if_failed(self):
        material = self._make_material()
        material.analysis_status = MaterialStatus.FAILED
        material.analysis_retry_count = MAX_RETRY_COUNT
        material.save(update_fields=["analysis_status", "analysis_retry_count"])

        result = get_analysis_status(material)

        self.assertFalse(result["can_retry"])
        self.assertIsNone(result["retry_after_seconds"])

    def test_can_retry_false_when_completed(self):
        material = self._make_material()
        material.analysis_status = MaterialStatus.COMPLETED

        result = get_analysis_status(material)

        self.assertFalse(result["can_retry"])
        self.assertIsNone(result["retry_after_seconds"])

    def test_can_retry_false_when_pending(self):
        material = self._make_material()
        material.analysis_status = MaterialStatus.PENDING

        result = get_analysis_status(material)

        self.assertFalse(result["can_retry"])
        self.assertIsNone(result["retry_after_seconds"])

    def test_can_retry_false_when_extraction_not_completed(self):
        material = self._make_material()
        material.status = MaterialStatus.PROCESSING
        material.analysis_status = MaterialStatus.FAILED
        material.save(update_fields=["status", "analysis_status"])

        result = get_analysis_status(material)

        self.assertFalse(result["can_retry"])

    def test_save_discards_result_when_run_superseded(self):
        material = self._make_material()
        run_id = _start_processing(material, is_retry=False)
        self.assertIsNotNone(run_id)

        StudyMaterial.objects.filter(pk=material.pk).update(analysis_run_id=uuid.uuid4())

        fake_tasks = [
            task_extractor.ExtractedTask(
                unit_name="1장", title="가짜 작업", task_type="concept",
                importance="high", depth="core", difficulty="normal",
                ai_reason="테스트용",
            )
        ]

        with self.assertRaises(StaleAnalysisRunError):
            _save_tasks_with_estimates(material, fake_tasks, material.extracted_text, run_id)

        self.assertEqual(StudyTask.objects.filter(study_material=material).count(), 0)

    def test_finish_failure_raises_when_run_superseded(self):
        material = self._make_material()
        old_run_id = _start_processing(material, is_retry=False)

        new_run_id = uuid.uuid4()
        StudyMaterial.objects.filter(pk=material.pk).update(
            analysis_status=MaterialStatus.PROCESSING, analysis_run_id=new_run_id,
        )

        with self.assertRaises(StaleAnalysisRunError):
            _finish_failure(material, "예전 실행의 실패 메시지", old_run_id)

        material.refresh_from_db()
        self.assertEqual(material.analysis_status, MaterialStatus.PROCESSING)
        self.assertIsNone(material.analysis_error_message)
        self.assertEqual(material.analysis_run_id, new_run_id)

    def test_new_run_after_zombie_gets_fresh_run_id(self):
        material = self._make_stale_processing(retry_count=0)
        old_run_id = material.analysis_run_id

        new_run_id = _start_processing(material, is_retry=True)

        self.assertIsNotNone(new_run_id)
        self.assertNotEqual(new_run_id, old_run_id)

    def test_end_to_end_zombie_takeover_new_run_wins(self):
        material = self._make_stale_processing(retry_count=0)
        old_run_id = material.analysis_run_id

        tasks = retry_analysis(material)

        material.refresh_from_db()
        self.assertTrue(len(tasks) > 0)
        self.assertEqual(material.analysis_status, MaterialStatus.COMPLETED)
        new_run_id = material.analysis_run_id
        self.assertNotEqual(new_run_id, old_run_id)

        with self.assertRaises(StaleAnalysisRunError):
            _finish_failure(material, "예전 실행의 뒤늦은 실패", old_run_id)

        material.refresh_from_db()
        self.assertEqual(material.analysis_status, MaterialStatus.COMPLETED)
        self.assertIsNone(material.analysis_error_message)

    def test_studytask_save_and_completion_are_rolled_back_together(self):
        material = self._make_material()
        original_bulk_update = StudyTask.objects.bulk_update

        def hijacking_bulk_update(objs, fields, **kwargs):
            result = original_bulk_update(objs, fields, **kwargs)
            StudyMaterial.objects.filter(pk=material.pk).update(
                analysis_run_id=uuid.uuid4()
            )
            return result

        with patch.object(StudyTask.objects, "bulk_update", side_effect=hijacking_bulk_update):
            with self.assertRaises(StaleAnalysisRunError):
                analyze_and_estimate(material)

        self.assertEqual(StudyTask.objects.filter(study_material=material).count(), 0)
        material.refresh_from_db()
        self.assertNotEqual(material.analysis_status, MaterialStatus.COMPLETED)

    @patch("exams.services.analysis_orchestrator._run_analysis_and_estimate")
    def test_execute_analysis_reports_stale_run_instead_of_original_failure(self, mock_run):
        material = self._make_material()
        run_id = _start_processing(material, is_retry=False)

        mock_run.side_effect = AICallFailedError("네트워크 오류")
        StudyMaterial.objects.filter(pk=material.pk).update(analysis_run_id=uuid.uuid4())

        with self.assertRaises(StaleAnalysisRunError):
            _execute_analysis(material, run_id)

        material.refresh_from_db()
        self.assertIsNone(material.analysis_error_message)


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
        self.assertEqual(data["analysis_error_message"], "네트워크 오류")
        self.assertEqual(data["retry_count"], 1)
        self.assertEqual(data["retry_remaining"], 1)

    def test_other_user_cannot_view_analysis_status(self):
        self.client.force_login(self.other)
        response = self.client.get(reverse('exams:material_analysis_status', args=[self.material.id]))
        self.assertEqual(response.status_code, 404)

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
        self.material.status = MaterialStatus.PROCESSING
        self.material.analysis_status = MaterialStatus.FAILED
        self.material.save(update_fields=["status", "analysis_status"])

        data = self._get_stage()

        self.assertEqual(data["stage"], "EXTRACTING")
        self.assertIsNone(data["failed_stage"])

    def test_stage_response_still_includes_existing_fields(self):
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
        self.material.status = MaterialStatus.FAILED
        self.material.error_message = "PDF 추출 실패 사유"
        self.material.save(update_fields=["status", "error_message"])

        data = self._get_stage()

        self.assertEqual(data["extraction_status"], MaterialStatus.FAILED)
        self.assertEqual(data["extraction_error_message"], "PDF 추출 실패 사유")

    def test_stage_response_includes_can_retry_true_when_failed_with_retries_left(self):
        self.material.analysis_status = MaterialStatus.FAILED
        self.material.analysis_retry_count = 0
        self.material.save(update_fields=["analysis_status", "analysis_retry_count"])

        data = self._get_stage()

        self.assertTrue(data["can_retry"])
        self.assertIsNone(data["retry_after_seconds"])

    def test_stage_response_includes_can_retry_false_within_5min_processing(self):
        self.material.analysis_status = MaterialStatus.PROCESSING
        self.material.analysis_started_at = timezone.now() - datetime.timedelta(minutes=1)
        self.material.save(update_fields=["analysis_status", "analysis_started_at"])

        data = self._get_stage()

        self.assertFalse(data["can_retry"])
        self.assertIsNotNone(data["retry_after_seconds"])
        self.assertTrue(200 <= data["retry_after_seconds"] <= 240)

    def test_stage_response_includes_can_retry_true_when_processing_over_5min(self):
        self.material.analysis_status = MaterialStatus.PROCESSING
        self.material.analysis_started_at = (
            timezone.now() - datetime.timedelta(seconds=PROCESSING_TIMEOUT_SECONDS + 1)
        )
        self.material.save(update_fields=["analysis_status", "analysis_started_at"])

        data = self._get_stage()

        self.assertTrue(data["can_retry"])
        self.assertIsNone(data["retry_after_seconds"])

    def test_stage_response_includes_is_stale_true_when_zombie(self):
        self.material.analysis_status = MaterialStatus.PROCESSING
        self.material.analysis_started_at = (
            timezone.now() - datetime.timedelta(seconds=PROCESSING_TIMEOUT_SECONDS + 1)
        )
        self.material.save(update_fields=["analysis_status", "analysis_started_at"])

        data = self._get_stage()

        self.assertTrue(data["is_stale"])

    def test_stage_response_includes_is_stale_false_when_fresh_processing(self):
        self.material.analysis_status = MaterialStatus.PROCESSING
        self.material.analysis_started_at = timezone.now() - datetime.timedelta(minutes=1)
        self.material.save(update_fields=["analysis_status", "analysis_started_at"])

        data = self._get_stage()

        self.assertFalse(data["is_stale"])

    def test_stage_response_distinguishes_zombie_with_retries_exhausted(self):
        self.material.analysis_status = MaterialStatus.PROCESSING
        self.material.analysis_retry_count = MAX_RETRY_COUNT
        self.material.analysis_started_at = (
            timezone.now() - datetime.timedelta(seconds=PROCESSING_TIMEOUT_SECONDS + 1)
        )
        self.material.save(update_fields=[
            "analysis_status", "analysis_retry_count", "analysis_started_at",
        ])

        data = self._get_stage()

        self.assertEqual(data["stage"], "ANALYZING")
        self.assertTrue(data["is_stale"])
        self.assertFalse(data["can_retry"])
        self.assertIsNone(data["retry_after_seconds"])

    def test_stage_response_includes_can_retry_false_when_retries_exhausted(self):
        self.material.analysis_status = MaterialStatus.FAILED
        self.material.analysis_retry_count = MAX_RETRY_COUNT
        self.material.save(update_fields=["analysis_status", "analysis_retry_count"])

        data = self._get_stage()

        self.assertFalse(data["can_retry"])

    def test_stage_response_includes_can_retry_false_when_completed(self):
        self.material.status = MaterialStatus.COMPLETED
        self.material.analysis_status = MaterialStatus.COMPLETED
        self.material.save(update_fields=["status", "analysis_status"])

        data = self._get_stage()

        self.assertFalse(data["can_retry"])

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

class AvailableTimeUpdateRedirectTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='tester',
            email='tester@example.com',
            password='testpass123',
        )
        self.client.force_login(self.user)

        self.period = ExamPeriod.objects.create(
            user=self.user,
            title='테스트 기간',
            start_date='2026-09-01',
            end_date='2026-09-05',
        )
        self.available_time = AvailableTime.objects.create(
            exam_period=self.period,
            date='2026-09-01',
            available_minutes=0,
        )
        self.url = reverse(
            'exams:available_time_update', kwargs={'period_id': self.period.id}
        )

    def _management_form_data(self):
        return {
            'form-TOTAL_FORMS': '1',
            'form-INITIAL_FORMS': '1',
            'form-MIN_NUM_FORMS': '0',
            'form-MAX_NUM_FORMS': '1000',
        }

    def _valid_formset_data(self):
        data = self._management_form_data()
        data.update({
            'form-0-id': str(self.available_time.id),
            'form-0-date': '2026-09-01',
            'form-0-hours': '2',
            'form-0-minutes': '30',
        })
        return data

    def _invalid_formset_data(self):
    # date는 disabled 필드이므로 date 누락이 아니라
    # hours 값을 잘못 보내서 formset invalid 유도
        data = self._management_form_data()
        data.update({
            'form-0-id': str(self.available_time.id),
            'form-0-date': '2026-09-01',
            'form-0-hours': '-1',
            'form-0-minutes': '30',
        })
        return data

    # 1. 정상 next 복귀
    def test_valid_next_redirects_back(self):
        data = self._valid_formset_data()
        data['next'] = '/dashboard/'

        response = self.client.post(self.url, data)

        self.assertRedirects(
            response, '/dashboard/', fetch_redirect_response=False
        )

    # 2. 외부 URL next 차단 -> period_detail로 폴백
    def test_external_next_is_blocked(self):
        data = self._valid_formset_data()
        data['next'] = 'https://evil.com/steal'

        response = self.client.post(self.url, data)

        expected = reverse(
            'exams:period_detail', kwargs={'period_id': self.period.id}
        )
        self.assertRedirects(response, expected, fetch_redirect_response=False)

    # 3. formset invalid 후에도 기존 next 유지
    def test_next_preserved_after_invalid_formset(self):
        data = self._invalid_formset_data()
        data['next'] = '/dashboard/'

        response = self.client.post(self.url, data)

        # invalid라서 리다이렉트가 아니라 200으로 폼 재렌더
        self.assertEqual(response.status_code, 200)
        # 재렌더된 hidden input에 next 값이 그대로 살아있는지 확인
        self.assertContains(response, 'name="next" value="/dashboard/"')

    # (보너스) next 없이 GET 진입 시 Referer로 채워지는지
    def test_next_falls_back_to_referer_on_get(self):
        response = self.client.get(self.url, HTTP_REFERER='/some/page/')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="next" value="/some/page/"')


class PeriodManageTests(TestCase):
    """
    시험기간 관리 허브(period_manage 계열) 접근 제어 + 가용시간 저장 정책
    (과거/마감 날짜 서버단 차단, DailyPlan.available_minutes 동기화, 시험일
    입력 허용) 확인.
    """

    def setUp(self):
        self.owner = User.objects.create_user(
            username='pm_owner@example.com', email='pm_owner@example.com', password='pass1234!'
        )
        self.other = User.objects.create_user(
            username='pm_other@example.com', email='pm_other@example.com', password='pass1234!'
        )
        self.today = timezone.localdate()
        self.period = ExamPeriod.objects.create(
            user=self.owner, title='관리허브테스트',
            start_date=self.today, end_date=self.today + datetime.timedelta(days=5),
            status=ExamPeriodStatus.ACTIVE,
        )
        self.exam = Exam.objects.create(
            exam_period=self.period, subject_name='신호및시스템',
            exam_date=self.today + datetime.timedelta(days=5),
        )
        for i in range(6):
            AvailableTime.objects.create(
                exam_period=self.period,
                date=self.today + datetime.timedelta(days=i),
                available_minutes=100,
            )

    def _make_plan(self):
        from planner.models import DailyPlan, DailyPlanItem

        study_task = StudyTask.objects.create(
            exam=self.exam, title='1장', estimated_min_minutes=20, estimated_max_minutes=40,
        )
        daily_plan = DailyPlan.objects.create(
            exam_period=self.period, date=self.today,
            available_minutes=100, planned_minutes=40,
        )
        DailyPlanItem.objects.create(
            daily_plan=daily_plan, study_task=study_task, planned_minutes=40, order=1,
        )
        return daily_plan

    def _management_form_data(self, rows):
        data = {
            'form-TOTAL_FORMS': str(len(rows)),
            'form-INITIAL_FORMS': str(len(rows)),
            'form-MIN_NUM_FORMS': '0',
            'form-MAX_NUM_FORMS': '1000',
        }
        for i, at in enumerate(rows):
            data[f'form-{i}-id'] = str(at.id)
            data[f'form-{i}-date'] = str(at.date)
            data[f'form-{i}-hours'] = '3'
            data[f'form-{i}-minutes'] = '0'
        return data

    # ---- 접근 제어 ----

    def test_period_manage_redirects_when_no_plan(self):
        self.client.force_login(self.owner)
        response = self.client.get(
            reverse('exams:period_manage', kwargs={'period_id': self.period.id})
        )
        self.assertRedirects(
            response, reverse('exams:period_detail', kwargs={'period_id': self.period.id})
        )

    def test_period_manage_available_time_redirects_when_no_plan(self):
        self.client.force_login(self.owner)
        response = self.client.get(
            reverse('exams:period_manage_available_time', kwargs={'period_id': self.period.id})
        )
        self.assertRedirects(
            response, reverse('exams:period_detail', kwargs={'period_id': self.period.id})
        )

    def test_period_manage_task_view_redirects_when_no_plan(self):
        self.client.force_login(self.owner)
        response = self.client.get(
            reverse('exams:period_manage_task_view', kwargs={'exam_id': self.exam.id})
        )
        self.assertRedirects(
            response, reverse('exams:period_detail', kwargs={'period_id': self.period.id})
        )

    def test_period_manage_accessible_after_plan_generated(self):
        self._make_plan()
        self.client.force_login(self.owner)
        response = self.client.get(
            reverse('exams:period_manage', kwargs={'period_id': self.period.id})
        )
        self.assertEqual(response.status_code, 200)

    def test_other_user_cannot_access_period_manage(self):
        self._make_plan()
        self.client.force_login(self.other)
        response = self.client.get(
            reverse('exams:period_manage', kwargs={'period_id': self.period.id})
        )
        self.assertEqual(response.status_code, 404)

    def test_other_user_cannot_access_period_manage_available_time(self):
        self._make_plan()
        self.client.force_login(self.other)
        response = self.client.get(
            reverse('exams:period_manage_available_time', kwargs={'period_id': self.period.id})
        )
        self.assertEqual(response.status_code, 404)

    def test_other_user_cannot_access_period_manage_task_view(self):
        self._make_plan()
        self.client.force_login(self.other)
        response = self.client.get(
            reverse('exams:period_manage_task_view', kwargs={'exam_id': self.exam.id})
        )
        self.assertEqual(response.status_code, 404)

    # ---- 가용시간 저장 정책 ----

    def test_past_date_edit_rejected_server_side(self):
        self._make_plan()
        past_at = AvailableTime.objects.create(
            exam_period=self.period, date=self.today - datetime.timedelta(days=1),
            available_minutes=50,
        )
        self.client.force_login(self.owner)
        rows = list(AvailableTime.objects.filter(exam_period=self.period).order_by('date'))
        data = self._management_form_data(rows)
        idx = rows.index(past_at)
        data[f'form-{idx}-hours'] = '5'

        url = reverse('exams:period_manage_available_time', kwargs={'period_id': self.period.id})
        response = self.client.post(url, data)

        self.assertEqual(response.status_code, 200)  # 리다이렉트 안 됨 = 거부됨
        past_at.refresh_from_db()
        self.assertEqual(past_at.available_minutes, 50)

    def test_finalized_date_edit_rejected_server_side(self):
        daily_plan = self._make_plan()
        daily_plan.finalized_at = timezone.now()
        daily_plan.save(update_fields=['finalized_at'])

        self.client.force_login(self.owner)
        rows = list(AvailableTime.objects.filter(exam_period=self.period).order_by('date'))
        data = self._management_form_data(rows)
        today_at = AvailableTime.objects.get(exam_period=self.period, date=self.today)
        idx = rows.index(today_at)
        data[f'form-{idx}-hours'] = '5'

        url = reverse('exams:period_manage_available_time', kwargs={'period_id': self.period.id})
        response = self.client.post(url, data)

        self.assertEqual(response.status_code, 200)
        today_at.refresh_from_db()
        self.assertEqual(today_at.available_minutes, 100)

    def test_available_time_edit_syncs_daily_plan(self):
        daily_plan = self._make_plan()
        self.client.force_login(self.owner)
        rows = list(AvailableTime.objects.filter(exam_period=self.period).order_by('date'))
        data = self._management_form_data(rows)
        today_at = AvailableTime.objects.get(exam_period=self.period, date=self.today)
        idx = rows.index(today_at)
        data[f'form-{idx}-hours'] = '2'
        data[f'form-{idx}-minutes'] = '0'

        url = reverse('exams:period_manage_available_time', kwargs={'period_id': self.period.id})
        response = self.client.post(url, data)

        self.assertRedirects(
            response, reverse('exams:period_manage', kwargs={'period_id': self.period.id})
        )
        daily_plan.refresh_from_db()
        self.assertEqual(daily_plan.available_minutes, 120)

    def test_exam_day_is_editable(self):
        """스케줄러는 '그 과목 자신의 시험일'만 배치 금지라, 시험일도
        가용시간 입력은 가능해야 한다 (다른 과목 공부에 쓸 수 있음)."""
        self._make_plan()
        exam_day_at = AvailableTime.objects.get(exam_period=self.period, date=self.exam.exam_date)
        self.client.force_login(self.owner)

        rows = list(AvailableTime.objects.filter(exam_period=self.period).order_by('date'))
        data = self._management_form_data(rows)
        idx = rows.index(exam_day_at)
        data[f'form-{idx}-hours'] = '1'
        data[f'form-{idx}-minutes'] = '30'

        url = reverse('exams:period_manage_available_time', kwargs={'period_id': self.period.id})
        response = self.client.post(url, data)

        self.assertRedirects(
            response, reverse('exams:period_manage', kwargs={'period_id': self.period.id})
        )
        exam_day_at.refresh_from_db()
        self.assertEqual(exam_day_at.available_minutes, 90)

    # ---- 배정된 시간보다 적게 줄이는 것 차단 (#147) ----

    def test_reducing_below_planned_minutes_rejected_server_side(self):
        self._make_plan()  # 오늘 planned_minutes=40
        self.client.force_login(self.owner)
        rows = list(AvailableTime.objects.filter(exam_period=self.period).order_by('date'))
        data = self._management_form_data(rows)
        today_at = AvailableTime.objects.get(exam_period=self.period, date=self.today)
        idx = rows.index(today_at)
        data[f'form-{idx}-hours'] = '0'
        data[f'form-{idx}-minutes'] = '20'  # 40분보다 적음

        url = reverse('exams:period_manage_available_time', kwargs={'period_id': self.period.id})
        response = self.client.post(url, data)

        self.assertEqual(response.status_code, 200)  # 리다이렉트 안 됨 = 거부됨
        today_at.refresh_from_db()
        self.assertEqual(today_at.available_minutes, 100)
        self.assertContains(response, "이미 40분이 배정되어 있어")

    def test_reducing_to_exactly_planned_minutes_allowed(self):
        self._make_plan()  # 오늘 planned_minutes=40
        self.client.force_login(self.owner)
        rows = list(AvailableTime.objects.filter(exam_period=self.period).order_by('date'))
        data = self._management_form_data(rows)
        today_at = AvailableTime.objects.get(exam_period=self.period, date=self.today)
        idx = rows.index(today_at)
        data[f'form-{idx}-hours'] = '0'
        data[f'form-{idx}-minutes'] = '40'  # 배정된 시간과 정확히 같음 -> 허용

        url = reverse('exams:period_manage_available_time', kwargs={'period_id': self.period.id})
        response = self.client.post(url, data)

        self.assertRedirects(
            response, reverse('exams:period_manage', kwargs={'period_id': self.period.id})
        )
        today_at.refresh_from_db()
        self.assertEqual(today_at.available_minutes, 40)

    def test_reducing_below_planned_minutes_on_day_without_plan_allowed(self):
        """DailyPlan이 아직 없는 날짜는 배정된 시간 자체가 없으니 자유롭게 줄일 수 있다."""
        self._make_plan()  # 오늘만 DailyPlan 생김
        self.client.force_login(self.owner)
        rows = list(AvailableTime.objects.filter(exam_period=self.period).order_by('date'))
        data = self._management_form_data(rows)
        tomorrow_at = AvailableTime.objects.get(
            exam_period=self.period, date=self.today + datetime.timedelta(days=1)
        )
        idx = rows.index(tomorrow_at)
        data[f'form-{idx}-hours'] = '0'
        data[f'form-{idx}-minutes'] = '10'

        url = reverse('exams:period_manage_available_time', kwargs={'period_id': self.period.id})
        response = self.client.post(url, data)

        self.assertRedirects(
            response, reverse('exams:period_manage', kwargs={'period_id': self.period.id})
        )
        tomorrow_at.refresh_from_db()
        self.assertEqual(tomorrow_at.available_minutes, 10)


class SourcePagesTestCase(TestCase):
    """
    PDF 페이지 번호 추적 기능 (pdf_extractor.py가 "--- 페이지 N ---" 경계
    표시를 남기고, task_extractor.py가 각 작업의 근거가 된 모든 페이지를
    AI 응답에서 읽어 StudyTask.source_pages(리스트)에 저장한다).

    "대표 페이지 하나"가 아니라 "관련된 모든 페이지"를 담는 방식으로 확정했다
    (한 작업이 여러 페이지 내용을 종합한 경우가 많아서 하나만 고르면 정보 손실).
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="source_pages_tester@example.com",
            email="source_pages_tester@example.com", password="pass1234!",
        )
        self.period = ExamPeriod.objects.create(
            user=self.user, title="페이지 추적 테스트",
            start_date=datetime.date(2026, 8, 1), end_date=datetime.date(2026, 8, 20),
        )
        self.exam = Exam.objects.create(
            exam_period=self.period, subject_name="자료구조", exam_date=datetime.date(2026, 8, 18),
        )

    # ---------- _parse_source_pages 단위 테스트 ----------

    def test_parse_source_pages_accepts_int_list(self):
        self.assertEqual(task_extractor._parse_source_pages([4, 5, 6]), [4, 5, 6])

    def test_parse_source_pages_dedupes_and_sorts(self):
        self.assertEqual(task_extractor._parse_source_pages([9, 5, 5, 6, 9]), [5, 6, 9])

    def test_parse_source_pages_accepts_numeric_strings(self):
        self.assertEqual(task_extractor._parse_source_pages(["12", "7"]), [7, 12])

    def test_parse_source_pages_empty_when_missing(self):
        self.assertEqual(task_extractor._parse_source_pages(None), [])

    def test_parse_source_pages_empty_when_not_a_list(self):
        # 응답 형식이 완전히 틀어져서 리스트가 아닌 값이 온 경우
        self.assertEqual(task_extractor._parse_source_pages(4), [])
        self.assertEqual(task_extractor._parse_source_pages("4"), [])

    def test_parse_source_pages_drops_invalid_elements_but_keeps_valid_ones(self):
        # 개별 원소가 이상해도 전체를 버리지 않고 유효한 것만 취한다
        self.assertEqual(
            task_extractor._parse_source_pages([5, "페이지", -1, 0, True, "8", None]),
            [5, 8],
        )

    def test_parse_source_pages_empty_when_all_invalid(self):
        self.assertEqual(task_extractor._parse_source_pages(["없음", -3, False]), [])

    # ---------- _parse_and_validate가 source_pages를 채우는지 ----------

    def test_parse_and_validate_fills_source_pages_from_response(self):
        raw = """{
            "tasks": [
                {"unit_name": "1장", "title": "개념 정리", "task_type": "concept",
                 "importance": "high", "depth": "core", "difficulty": "normal",
                 "ai_reason": "테스트", "source_pages": [5, 6, 9]}
            ]
        }"""
        tasks = task_extractor._parse_and_validate(raw)
        self.assertEqual(tasks[0].source_pages, [5, 6, 9])

    def test_parse_and_validate_source_pages_optional(self):
        """source_pages 키 자체가 없어도(텍스트 직접 입력 등) 검증 실패로 취급하지 않는다."""
        raw = """{
            "tasks": [
                {"unit_name": "1장", "title": "개념 정리", "task_type": "concept",
                 "importance": "high", "depth": "core", "difficulty": "normal",
                 "ai_reason": "테스트"}
            ]
        }"""
        tasks = task_extractor._parse_and_validate(raw)
        self.assertEqual(tasks[0].source_pages, [])

    # ---------- mock 모드 기준 end-to-end ----------

    @override_settings(AI_MOCK_MODE=True)
    def test_fetch_extracted_tasks_returns_source_pages_from_mock(self):
        tasks = task_extractor.fetch_extracted_tasks(
            self.exam, "1장 --- 페이지 4 --- 내용 --- 페이지 5 --- 더 내용"
        )
        # _MOCK_RESPONSE 기준: 첫 작업은 [4, 5], 두 번째는 빈 리스트
        self.assertEqual(tasks[0].source_pages, [4, 5])
        self.assertEqual(tasks[1].source_pages, [])

    @override_settings(AI_MOCK_MODE=True)
    def test_save_extracted_tasks_persists_source_pages(self):
        material = StudyMaterial.objects.create(
            exam=self.exam, title="테스트 자료",
            extracted_text="--- 페이지 4 --- 1장 내용 --- 페이지 5 --- 더 내용",
            status=MaterialStatus.COMPLETED,
        )
        extracted_tasks = task_extractor.fetch_extracted_tasks(self.exam, material.extracted_text)
        saved = task_extractor.save_extracted_tasks(material, extracted_tasks)

        self.assertEqual(saved[0].source_pages, [4, 5])
        self.assertEqual(saved[1].source_pages, [])

        saved[0].refresh_from_db()
        self.assertEqual(saved[0].source_pages, [4, 5])

    # ---------- pdf_extractor.py가 만드는 실제 마커 형식과 진짜로 연동되는지 ----------

    def test_page_marker_format_matches_pdf_extractor_output(self):
        """
        pdf_extractor.extract_text_from_pdf()를 실제로 호출해서 나온 텍스트를
        그대로 task_extractor.build_prompt()에 넣어봐서, 두 서비스(BE2/BE3)
        사이의 연동 지점(페이지 마커 형식)이 실제로 맞물리는지 확인한다.
        형식이 하드코딩된 문자열 비교가 아니라 실제 pdf_extractor 출력 기준이라
        pdf_extractor.py 쪽 마커 형식이 나중에 바뀌면 이 테스트가 잡아준다.
        """
        from exams.services.pdf_extractor import extract_text_from_pdf

        raw_pdf_data = b"""%PDF-1.4
1 0 obj <</Type /Catalog /Pages 2 0 R>> endobj
2 0 obj <</Type /Pages /Kids [3 0 R] /Count 1>> endobj
3 0 obj <</Type /Page /Parent 2 0 R /Resources <</Font <</F1 4 0 R>>>> /MediaBox [0 0 612 792] /Contents 5 0 R>> endobj
4 0 obj <</Type /Font /Subtype /Type1 /BaseFont /Helvetica>> endobj
5 0 obj <</Length 55>> stream
BT
/F1 12 Tf
100 700 Td
(Circular Linked List concept) Tj
ET
endstream endobj
xref
0 6
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000115 00000 n 
0000000231 00000 n 
trailer <</Size 6 /Root 1 0 R>>
startxref
367
%%EOF"""
        pdf_file = SimpleUploadedFile("sample.pdf", raw_pdf_data, content_type="application/pdf")
        extracted_text = extract_text_from_pdf(pdf_file)

        # pdf_extractor가 실제로 "--- 페이지 1 ---" 마커를 붙였는지 확인
        self.assertIn("--- 페이지 1 ---", extracted_text)

        # 이 실제 출력을 그대로 task_extractor의 프롬프트에 넣었을 때
        # 페이지 마커가 원문 그대로 프롬프트에 살아있는지 확인
        prompt = task_extractor.build_prompt(
            self.exam.subject_name, self.exam.exam_date, extracted_text,
        )
        self.assertIn("--- 페이지 1 ---", prompt)
        self.assertIn('"--- 페이지 N ---"', prompt)  # 규칙 9번 안내 문구도 포함되는지

    # ---------- 결과 편차 완화(seed/response_schema) 실제 API 호출 설정 검증 ----------

    @override_settings(AI_MOCK_MODE=False, GOOGLE_API_KEY="fake-key-for-test")
    @patch("exams.services.task_extractor.genai.Client")
    def test_call_ai_passes_seed_and_response_schema(self, mock_client_cls):
        """
        리뷰 반영: 같은 자료를 여러 번 분석해도 작업 개수/분류가 흔들리는 문제를
        줄이기 위해 seed 고정 + response_schema 강제를 추가했다. 실제 API 호출
        설정(config)에 이 값들이 정확히 전달되는지 확인한다 (mock 모드가 아닌
        진짜 호출 경로를 patch로 가로채서 검증).
        """
        mock_response = type("Resp", (), {"text": task_extractor._MOCK_RESPONSE})()
        mock_client_instance = mock_client_cls.return_value
        mock_client_instance.models.generate_content.return_value = mock_response

        task_extractor._call_ai("테스트 프롬프트")

        mock_client_instance.models.generate_content.assert_called_once()
        _, call_kwargs = mock_client_instance.models.generate_content.call_args
        config = call_kwargs["config"]

        self.assertEqual(config.seed, task_extractor.GENERATION_SEED)
        self.assertEqual(config.response_schema, task_extractor._RESPONSE_SCHEMA)
        self.assertEqual(config.response_mime_type, "application/json")
        self.assertEqual(config.temperature, 0.2)

    def test_response_schema_matches_required_task_fields(self):
        """_RESPONSE_SCHEMA의 required 목록이 _REQUIRED_TASK_FIELDS와 어긋나지 않는지
        확인한다 (둘 중 하나만 고치고 다른 하나를 깜빡하는 실수를 방지)."""
        schema_required = set(
            task_extractor._RESPONSE_SCHEMA["properties"]["tasks"]["items"]["required"]
        )
        self.assertEqual(schema_required, task_extractor._REQUIRED_TASK_FIELDS)
    # ---------- 실제 문서 페이지 범위 검증 (리뷰 반영) ----------

    def test_extract_available_page_numbers_from_markers(self):
        text = "--- 페이지 4 --- 내용\n--- 페이지 7 --- 더 내용\n--- 페이지 9 --- 마지막"
        self.assertEqual(task_extractor._extract_available_page_numbers(text), {4, 7, 9})

    def test_extract_available_page_numbers_empty_when_no_markers(self):
        self.assertEqual(task_extractor._extract_available_page_numbers("그냥 텍스트입니다"), set())

    def test_parse_source_pages_filters_out_pages_not_in_valid_set(self):
        """
        리뷰 반영: response_schema는 "정수 배열"이라는 형식만 강제하지, 그 정수가
        실제 문서 범위 안의 페이지인지는 보장하지 않는다. 문서에 4, 5페이지만
        있는데 AI가 21페이지를 지어내 반환하면, 21은 걸러지고 4만 남아야 한다.
        """
        result = task_extractor._parse_source_pages([4, 21], valid_pages={4, 5})
        self.assertEqual(result, [4])

    def test_parse_source_pages_valid_pages_none_skips_check(self):
        """valid_pages를 안 넘기면(None) 기존처럼 범위 검증을 건너뛴다 (하위 호환)."""
        result = task_extractor._parse_source_pages([4, 21], valid_pages=None)
        self.assertEqual(result, [4, 21])

    def test_parse_and_validate_filters_out_of_range_pages(self):
        raw = """{
            "tasks": [
                {"unit_name": "1장", "title": "개념 정리", "task_type": "concept",
                 "importance": "high", "depth": "core", "difficulty": "normal",
                 "ai_reason": "테스트", "source_pages": [4, 5, 21]}
            ]
        }"""
        tasks = task_extractor._parse_and_validate(raw, valid_pages={4, 5})
        self.assertEqual(tasks[0].source_pages, [4, 5])

    @override_settings(AI_MOCK_MODE=False, GOOGLE_API_KEY="fake-key-for-test")
    @patch("exams.services.task_extractor.genai.Client")
    def test_fetch_extracted_tasks_end_to_end_rejects_page_out_of_document_range(self, mock_client_cls):
        """
        end-to-end: 문서에 4페이지만 있는데 AI가 [4, 21]을 반환하면, 존재하지
        않는 21페이지는 실제로 최종 결과에서 제거되어야 한다.
        """
        fake_ai_response = json.dumps({
            "tasks": [{
                "unit_name": "1장", "title": "개념 정리", "task_type": "concept",
                "importance": "high", "depth": "core", "difficulty": "normal",
                "ai_reason": "테스트", "source_pages": [4, 21],
            }]
        })
        mock_response = type("Resp", (), {"text": fake_ai_response})()
        mock_client_instance = mock_client_cls.return_value
        mock_client_instance.models.generate_content.return_value = mock_response

        tasks = task_extractor.fetch_extracted_tasks(self.exam, "--- 페이지 4 --- 1장 내용")

        self.assertEqual(tasks[0].source_pages, [4])


def make_file(name='sample.pdf'):
    return SimpleUploadedFile(name, b'dummy pdf content', content_type='application/pdf')
 
 
@override_settings(MEDIA_ROOT=TEMP_MEDIA_ROOT)
class StudyMaterialFileDeleteSignalTests(TestCase):
    """
    StudyMaterial.file이 post_delete 시그널을 통해
    (단독 삭제 / CASCADE / QuerySet 대량삭제 모든 경로에서) 정리되는지 검증.
    """
 
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEMP_MEDIA_ROOT, ignore_errors=True)
 
    def setUp(self):
        self.user = User.objects.create_user(username='tester', password='pw')
        self.exam_period = ExamPeriod.objects.create(
            user=self.user,
            title='2026 2학기 중간고사',
            start_date='2026-10-01',
            end_date='2026-10-10',
        )
        self.exam = Exam.objects.create(
            exam_period=self.exam_period,
            subject_name='자료구조',
            exam_date='2026-10-05',
        )
 
    def _create_material_with_file(self, filename='sample.pdf'):
        material = StudyMaterial.objects.create(
            exam=self.exam,
            title='1단원 요약',
            file=make_file(filename),
        )
        self.assertTrue(material.file.storage.exists(material.file.name))
        return material
 
    def test_delete_study_material_directly_removes_file(self):
        material = self._create_material_with_file()
        file_path = material.file.name
        storage = material.file.storage
 
        with self.captureOnCommitCallbacks(execute=True):
            material.delete()
 
        self.assertFalse(storage.exists(file_path))
 
    def test_delete_exam_cascades_and_removes_file(self):
        material = self._create_material_with_file()
        file_path = material.file.name
        storage = material.file.storage
 
        with self.captureOnCommitCallbacks(execute=True):
            self.exam.delete()
 
        self.assertFalse(storage.exists(file_path))
 
    def test_delete_exam_period_cascades_and_removes_file(self):
        material = self._create_material_with_file()
        file_path = material.file.name
        storage = material.file.storage
 
        with self.captureOnCommitCallbacks(execute=True):
            self.exam_period.delete()
 
        self.assertFalse(storage.exists(file_path))
 
    def test_bulk_queryset_delete_removes_file(self):
        """QuerySet.delete()로 대량삭제해도 post_delete 시그널이 걸려서 파일이 지워져야 함"""
        material = self._create_material_with_file()
        file_path = material.file.name
        storage = material.file.storage
 
        with self.captureOnCommitCallbacks(execute=True):
            StudyMaterial.objects.filter(pk=material.pk).delete()
 
        self.assertFalse(storage.exists(file_path))
 
    def test_material_without_file_deletes_without_error(self):
        material = StudyMaterial.objects.create(exam=self.exam, title='텍스트만 있는 자료')
 
        with self.captureOnCommitCallbacks(execute=True):
            material.delete()  # 에러 없이 통과해야 함
 
    def test_replacing_file_deletes_old_file(self):
        material = self._create_material_with_file('old.pdf')
        old_path = material.file.name
        storage = material.file.storage
 
        with self.captureOnCommitCallbacks(execute=True):
            material.file = make_file('new.pdf')
            material.save()
 
        self.assertFalse(storage.exists(old_path))
        self.assertTrue(storage.exists(material.file.name))
 
 
@override_settings(MEDIA_ROOT=TEMP_MEDIA_ROOT)
class StudyMaterialFileDeleteRollbackTests(TransactionTestCase):
    """
    실제 트랜잭션 커밋/롤백이 필요한 테스트라 TransactionTestCase 사용.
    (TestCase는 매 테스트를 롤백하는 방식이라 on_commit이 실제로 발생하지 않음)
    """
 
    def tearDown(self):
        shutil.rmtree(TEMP_MEDIA_ROOT, ignore_errors=True)
 
    def test_rollback_does_not_delete_file(self):
        user = User.objects.create_user(username='tester2', password='pw')
        exam_period = ExamPeriod.objects.create(
            user=user, title='기말고사', start_date='2026-12-01', end_date='2026-12-10'
        )
        exam = Exam.objects.create(
            exam_period=exam_period, subject_name='알고리즘', exam_date='2026-12-05'
        )
        material = StudyMaterial.objects.create(exam=exam, title='요약', file=make_file())
        material_pk = material.pk  # delete() 호출 시 material.pk가 None으로 바뀌므로 미리 저장
        file_path = material.file.name
        storage = material.file.storage
        self.assertTrue(storage.exists(file_path))
 
        class RollbackTriggered(Exception):
            pass
 
        try:
            with transaction.atomic():
                material.delete()
                raise RollbackTriggered('의도적으로 롤백 발생')
        except RollbackTriggered:
            pass
 
        # 트랜잭션이 롤백됐으므로 DB row도, 파일도 그대로 남아 있어야 함
        self.assertTrue(StudyMaterial.objects.filter(pk=material_pk).exists())
        self.assertTrue(storage.exists(file_path))

class PeriodDeleteWithPlanTests(TestCase):
    def setUp(self):
        # 1. 고유한 email과 함께 유저 생성
        self.user = User.objects.create_user(
            username='tester',
            email='tester@example.com',
            password='pass1234'
        )
        # 2. client.force_login으로 세션 유지
        self.client.force_login(self.user)

        self.period = ExamPeriod.objects.create(
            user=self.user,
            title='중간고사',
            start_date=datetime.date(2026, 8, 1),
            end_date=datetime.date(2026, 8, 10),
            status=ExamPeriodStatus.ACTIVE,
        )
        
        # 3. Exam 필수 필드(exam_date) 포함
        self.exam = Exam.objects.create(
            exam_period=self.period,
            subject_name='알고리즘',
            exam_date=datetime.date(2026, 8, 5),
        )
        
        self.task = StudyTask.objects.create(
            exam=self.exam,
            title='탐색 알고리즘',
        )

        # 4. 계획 생성 상태 재현 (DailyPlan + DailyPlanItem)
        self.plan = DailyPlan.objects.create(
            exam_period=self.period,
            date=datetime.date(2026, 8, 1),
            available_minutes=120,
            planned_minutes=60,
        )
        self.plan_item = DailyPlanItem.objects.create(
            daily_plan=self.plan,
            study_task=self.task,
            planned_minutes=60,
            order=1,
        )

    def test_direct_orm_delete_raises_protected_error(self):
        """
        회귀 방지: PROTECT 제약이 여전히 살아있는지 확인.
        (ExamPeriod를 거치지 않고 StudyTask만 바로 지우면 막혀야 함)
        """
        with self.assertRaises(ProtectedError):
            self.task.delete()

    def test_period_delete_view_cascades_successfully(self):
        """
        버그 수정 검증: 계획(DailyPlan, RecoveryPlan)이 생성된 시험기간을 뷰로 삭제 시 
        500(ProtectedError) 없이 관련 플랜 및 ExamPeriod가 정상적으로 모두 연쇄 삭제되어야 함.
        """
        # RecoveryPlan 및 RecoveryPlanItem 생성하여 복구안 연쇄 삭제 경로도 함께 재현
        recovery_plan = RecoveryPlan.objects.create(
            exam_period=self.period,
            source_daily_plan=self.plan,
            recovery_type=RecoveryType.MAINTAIN_VOLUME,
            status=RecoveryPlanStatus.PENDING,
        )
        recovery_plan_item = RecoveryPlanItem.objects.create(
            recovery_plan=recovery_plan,
            study_task=self.task,
            action_type=RecoveryActionType.RESCHEDULE,
        )

        period_id = self.period.id
        recovery_plan_id = recovery_plan.id
        recovery_plan_item_id = recovery_plan_item.id
        study_task_id = self.task.id

        url = reverse('exams:period_delete', args=[period_id])
        response = self.client.post(url)

        # 302 리다이렉트 및 목록 화면으로 이동 확인
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('exams:period_list'))

        # ExamPeriod, DailyPlan, RecoveryPlan 및 연결된 Item/Task 연쇄 삭제 확인
        self.assertFalse(ExamPeriod.objects.filter(id=period_id).exists())
        self.assertFalse(DailyPlan.objects.filter(exam_period=period_id).exists())
        self.assertFalse(DailyPlanItem.objects.filter(study_task_id=study_task_id).exists())
        self.assertFalse(RecoveryPlan.objects.filter(id=recovery_plan_id).exists())
        self.assertFalse(RecoveryPlanItem.objects.filter(id=recovery_plan_item_id).exists())
        self.assertFalse(StudyTask.objects.filter(id=study_task_id).exists())

    def test_period_delete_without_plan_still_works(self):
        """
        회귀 방지: 계획이 없는 일반적인 경우도 그대로 잘 지워지는지 확인.
        """
        period2 = ExamPeriod.objects.create(
            user=self.user,
            title='기말고사',
            start_date=datetime.date(2026, 12, 1),
            end_date=datetime.date(2026, 12, 10),
            status=ExamPeriodStatus.ACTIVE,
        )
        url = reverse('exams:period_delete', args=[period2.id])
        response = self.client.post(url)

        self.assertEqual(response.status_code, 302)
        self.assertFalse(ExamPeriod.objects.filter(id=period2.id).exists())

    def test_other_users_period_cannot_be_deleted(self):
        """
        권한 체크 회귀 방지: 다른 유저의 ExamPeriod 삭제 시 404가 발생해야 함.
        """
        other_user = User.objects.create_user(
            username='other',
            email='other@example.com',
            password='pass1234'
        )
        other_period = ExamPeriod.objects.create(
            user=other_user,
            title='남의 시험',
            start_date=datetime.date(2026, 9, 1),
            end_date=datetime.date(2026, 9, 5),
            status=ExamPeriodStatus.ACTIVE,
        )
        url = reverse('exams:period_delete', args=[other_period.id])
        response = self.client.post(url)

        self.assertEqual(response.status_code, 404)
        self.assertTrue(ExamPeriod.objects.filter(id=other_period.id).exists())

def _minutes_to_hm(total_minutes):
    return divmod(total_minutes, 60)


class AvailableTimeUpdatePastOrFinalizedBlockTest(TestCase):
    """
    정책:
    - 미래 날짜: 자유롭게 수정 가능
    - 과거 날짜 또는 이미 마감된(DailyPlan.finalized_at 존재) 날짜: 수정 차단
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="tester3",
            email="tester3@test.com",
            password="pw12345!",
        )

        login_result = self.client.login(
            email="tester3@test.com",
            password="pw12345!",
        )


        self.today = timezone.localdate()

        self.period = ExamPeriod.objects.create(
            user=self.user,
            title="쪽지시험",
            start_date=self.today - datetime.timedelta(days=2),
            end_date=self.today + datetime.timedelta(days=5),
            status=ExamPeriodStatus.ACTIVE,
        )

        self.past_date = self.today - datetime.timedelta(days=1)
        self.finalized_future_date = self.today + datetime.timedelta(days=1)
        self.open_future_date = self.today + datetime.timedelta(days=3)

        self.at_past = AvailableTime.objects.create(
            exam_period=self.period,
            date=self.past_date,
            available_minutes=30,
        )

        self.at_finalized_future = AvailableTime.objects.create(
            exam_period=self.period,
            date=self.finalized_future_date,
            available_minutes=60,
        )

        self.at_open_future = AvailableTime.objects.create(
            exam_period=self.period,
            date=self.open_future_date,
            available_minutes=60,
        )

        self.finalized_plan = DailyPlan.objects.create(
            exam_period=self.period,
            date=self.finalized_future_date,
            available_minutes=60,
            planned_minutes=60,
            finalized_at=timezone.now(),
        )

        self.open_plan = DailyPlan.objects.create(
            exam_period=self.period,
            date=self.open_future_date,
            available_minutes=60,
            planned_minutes=60,
        )

        self.url = reverse(
            "exams:available_time_update",
            args=[self.period.id],
        )
    def _build_data(self, overrides=None):
        overrides = overrides or {}
        queryset = AvailableTime.objects.filter(
            exam_period=self.period
        ).order_by("date")

        overrides_by_id = {}
        for at_obj, new_minutes in overrides.items():
            at_obj.refresh_from_db()
            overrides_by_id[at_obj.id] = new_minutes

        data = {
            "form-TOTAL_FORMS": str(queryset.count()),
            "form-INITIAL_FORMS": str(queryset.count()),
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
        }

        for i, obj in enumerate(queryset):
            target_minutes = overrides_by_id.get(
                obj.id, obj.available_minutes
            )
            hours, minutes = _minutes_to_hm(target_minutes)

            data[f"form-{i}-id"] = str(obj.id)
            data[f"form-{i}-date"] = obj.date.isoformat()
            data[f"form-{i}-hours"] = str(hours)
            data[f"form-{i}-minutes"] = str(minutes)

        return data



    def test_open_future_date_is_freely_editable(self):
        data = self._build_data({self.at_open_future: 100})
        response = self.client.post(self.url, data)

        self.assertEqual(response.status_code, 302)

        self.at_open_future.refresh_from_db()
        self.assertEqual(self.at_open_future.available_minutes, 100)

        self.open_plan.refresh_from_db()
        self.assertEqual(self.open_plan.available_minutes, 100)

    def test_finalized_future_date_is_blocked(self):
        original_minutes = self.at_finalized_future.available_minutes
        data = self._build_data({self.at_finalized_future: 999})
        response = self.client.post(self.url, data)

        self.assertEqual(response.status_code, 200)

        formset = response.context["formset"]
        self.assertTrue(formset.non_form_errors())

        self.at_finalized_future.refresh_from_db()
        self.assertEqual(self.at_finalized_future.available_minutes, original_minutes)

        self.finalized_plan.refresh_from_db()
        self.assertEqual(self.finalized_plan.available_minutes, 60)

    def test_past_date_is_blocked(self):
        original_minutes = self.at_past.available_minutes
        data = self._build_data({self.at_past: 999})
        response = self.client.post(self.url, data)

        self.assertEqual(response.status_code, 200)

        self.at_past.refresh_from_db()
        self.assertEqual(self.at_past.available_minutes, original_minutes)

    def test_finalized_date_blocks_entire_submission(self):
        data = self._build_data({
            self.at_finalized_future: 999,
            self.at_open_future: 100,
        })
        self.client.post(self.url, data)

        self.at_open_future.refresh_from_db()
        self.assertEqual(self.at_open_future.available_minutes, 60)

    def test_future_date_without_daily_plan_is_editable(self):
        no_plan_date = self.today + datetime.timedelta(days=4)
        at_no_plan = AvailableTime.objects.create(
            exam_period=self.period, date=no_plan_date, available_minutes=30
        )
        data = self._build_data({at_no_plan: 45})
        response = self.client.post(self.url, data)

        self.assertEqual(response.status_code, 302)

        at_no_plan.refresh_from_db()
        self.assertEqual(at_no_plan.available_minutes, 45)


class ExamPeriodLockValidationTests(TestCase):
    """계획이 생성된 시험기간에 대한 서버단 수정/삭제/Task/AI 경로 방어 락 검증"""

    def setUp(self):
        self.user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="password123",
        )

        self.client.login(
            email="test@example.com",
            password="password123",
        )

        self.period = ExamPeriod.objects.create(
            user=self.user,
            title="중간고사",
            start_date=datetime.date(2026, 5, 1),
            end_date=datetime.date(2026, 5, 10),
            status=ExamPeriodStatus.ACTIVE,
        )

        self.exam = Exam.objects.create(
            exam_period=self.period,
            subject_name="알고리즘",
            exam_date=datetime.date(2026, 5, 5),
            priority=PriorityLevel.HIGH,
        )

        self.material = StudyMaterial.objects.create(
            exam=self.exam,
            title="1주차 자료",
            material_type=MaterialType.TEXT,
            extracted_text="테스트 내용",
            status=MaterialStatus.COMPLETED,
        )

        self.task = StudyTask.objects.create(
            exam=self.exam,
            study_material=self.material,
            unit_name="1장",
            title="그래프 탐색",
            task_type=TaskType.CONCEPT,
            difficulty=TaskDifficulty.NORMAL,
            estimated_min_minutes=30,
            estimated_max_minutes=60,
        )

        # 계획이 이미 생성된 시험기간 (DailyPlan + DailyPlanItem으로 PROTECT 제약 관계 형성)
        self.daily_plan = DailyPlan.objects.create(
            exam_period=self.period,
            date=datetime.date(2026, 5, 1),
            available_minutes=120,
            planned_minutes=120,
            status=DailyPlanStatus.PLANNED,
        )

        self.plan_item = DailyPlanItem.objects.create(
            daily_plan=self.daily_plan,
            study_task=self.task,
            planned_minutes=60,
            order=1,
            status=DailyPlanStatus.PLANNED,
        )

    # ============================================================
    # 계획이 존재하는 경우 - GET 요청 허용 검증 (피드백 2번)
    # ============================================================

    def test_get_requests_allowed_when_plan_exists(self):
        """계획이 생성되어 있어도 조회 목적의 GET 요청은 리다이렉트 없이 200 OK 응답해야 한다."""
        urls = [
            reverse("exams:period_update", args=[self.period.id]),
            reverse("exams:subject_create", args=[self.period.id]),
            reverse("exams:subject_update", args=[self.period.id, self.exam.id]),
            reverse("exams:material_create", args=[self.exam.id]),
            reverse("exams:task_review", args=[self.exam.id]),
            reverse("exams:task_create", args=[self.exam.id]),
        ]

        for url in urls:
            response = self.client.get(url)
            self.assertEqual(
                response.status_code, 200,
                f"GET 요청이 차단됨: {url}"
            )

    # ============================================================
    # 계획이 존재하는 경우 - 시험기간 POST 차단
    # ============================================================

    def test_period_update_blocked_when_plan_exists(self):
        """계획이 생성된 시험기간은 수정할 수 없다."""
        url = reverse("exams:period_update", args=[self.period.id])

        response = self.client.post(
            url,
            {
                "title": "수정된 중간고사",
                "start_date": "2026-05-01",
                "end_date": "2026-05-10",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.period.refresh_from_db()
        self.assertEqual(self.period.title, "중간고사")

        messages_list = list(response.wsgi_request._messages)
        self.assertTrue(
            any("수정하거나 삭제할 수 없습니다" in str(m) for m in messages_list)
        )

    # ============================================================
    # 계획이 존재하는 경우 - 과목 POST 차단
    # ============================================================

    def test_subject_create_blocked_when_plan_exists(self):
        """계획이 생성된 시험기간에는 새로운 과목을 추가할 수 없다."""
        url = reverse("exams:subject_create", args=[self.period.id])

        response = self.client.post(
            url,
            {
                "subject_name": "자료구조",
                "exam_date": "2026-05-06",
                "priority": PriorityLevel.HIGH,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            Exam.objects.filter(exam_period=self.period, subject_name="자료구조").exists()
        )

        messages_list = list(response.wsgi_request._messages)
        self.assertTrue(
            any("수정하거나 삭제할 수 없습니다" in str(m) for m in messages_list)
        )

    def test_subject_update_blocked_when_plan_exists(self):
        """계획이 생성된 시험기간의 과목은 수정할 수 없다."""
        url = reverse("exams:subject_update", args=[self.period.id, self.exam.id])

        response = self.client.post(
            url,
            {
                "subject_name": "수정된 알고리즘",
                "exam_date": "2026-05-06",
                "priority": PriorityLevel.HIGH,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.exam.refresh_from_db()
        self.assertEqual(self.exam.subject_name, "알고리즘")

        messages_list = list(response.wsgi_request._messages)
        self.assertTrue(
            any("수정하거나 삭제할 수 없습니다" in str(m) for m in messages_list)
        )

    def test_subject_delete_blocked_when_plan_exists(self):
        """계획이 생성된 시험기간의 과목은 삭제할 수 없다."""
        url = reverse("exams:subject_delete", args=[self.period.id, self.exam.id])

        response = self.client.post(url)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Exam.objects.filter(id=self.exam.id).exists())

        messages_list = list(response.wsgi_request._messages)
        self.assertTrue(
            any("수정하거나 삭제할 수 없습니다" in str(m) for m in messages_list)
        )

    # ============================================================
    # 계획이 존재하는 경우 - 학습자료 POST 차단
    # ============================================================

    def test_material_create_blocked_when_plan_exists(self):
        """계획이 생성된 시험기간에는 학습자료를 추가할 수 없다."""
        url = reverse("exams:material_create", args=[self.exam.id])

        response = self.client.post(
            url,
            {
                "title": "새로운 자료",
                "material_type": MaterialType.TEXT,
                "extracted_text": "새로운 테스트 내용",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            StudyMaterial.objects.filter(exam=self.exam, title="새로운 자료").exists()
        )

        messages_list = list(response.wsgi_request._messages)
        self.assertTrue(
            any("수정하거나 삭제할 수 없습니다" in str(m) for m in messages_list)
        )

    def test_material_delete_blocked_when_plan_exists(self):
        """계획이 생성된 시험기간의 학습자료는 삭제할 수 없다."""
        url = reverse("exams:material_delete", args=[self.material.id])

        response = self.client.post(url)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(StudyMaterial.objects.filter(id=self.material.id).exists())

        messages_list = list(response.wsgi_request._messages)
        self.assertTrue(
            any("수정하거나 삭제할 수 없습니다" in str(m) for m in messages_list)
        )

    # ============================================================
    # 피드백 3번 반영 - Task 관련 잠금 및 ProtectedError 방지 검증
    # ============================================================

    def test_task_review_formset_delete_blocked_and_protected_error_prevented(self):
        """
        DailyPlanItem에 연결된 StudyTask를 task_review formset에서 삭제 시도할 때:
        1. 500(ProtectedError) 없이 302 리다이렉트된다.
        2. StudyTask 및 DailyPlanItem이 삭제되지 않고 유지된다.
        """
        url = reverse("exams:task_review", args=[self.exam.id])
        post_data = {
            "form-TOTAL_FORMS": "1",
            "form-INITIAL_FORMS": "1",
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
            "form-0-id": str(self.task.id),
            "form-0-unit_name": self.task.unit_name,
            "form-0-title": self.task.title,
            "form-0-task_type": self.task.task_type,
            "form-0-difficulty": self.task.difficulty,
            "form-0-DELETE": "on",  # formset 삭제 요청
            "action": "save",
        }

        response = self.client.post(url, post_data)

        # 500이 터지지 않고 정상 리다이렉트
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse("exams:period_detail", args=[self.period.id]))

        # StudyTask와 DailyPlanItem이 안전하게 남아있는지 확인
        self.assertTrue(StudyTask.objects.filter(id=self.task.id).exists())
        self.assertTrue(DailyPlanItem.objects.filter(id=self.plan_item.id).exists())

        messages_list = list(response.wsgi_request._messages)
        self.assertTrue(
            any("수정하거나 삭제할 수 없습니다" in str(m) for m in messages_list)
        )

    def test_study_task_create_blocked_when_plan_exists(self):
        """계획이 생성된 시험기간에는 새로운 학습 작업을 직접 추가할 수 없다."""
        url = reverse("exams:task_create", args=[self.exam.id])
        post_data = {
            "unit_name": "2장",
            "title": "DFS 구현",
            "task_type": TaskType.CONCEPT,
            "importance": PriorityLevel.MEDIUM,
            "depth": TaskDepth.BASIC,
            "difficulty": TaskDifficulty.NORMAL,
        }

        response = self.client.post(url, post_data)

        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse("exams:period_detail", args=[self.period.id]))
        self.assertFalse(StudyTask.objects.filter(title="DFS 구현").exists())

    def test_study_task_confirm_blocked_when_plan_exists(self):
        """계획이 생성된 시험기간의 학습 작업 확정 POST 요청은 차단된다."""
        url = reverse("exams:task_confirm", args=[self.exam.id])

        response = self.client.post(url)

        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse("exams:period_detail", args=[self.period.id]))

    # ============================================================
    # 피드백 4번 반영 - AI 분석 관련 POST 경로 잠금 검증
    # ============================================================

    def test_ai_analysis_post_routes_blocked_when_plan_exists(self):
        """
        계획 생성 후 material_extract, material_analyze, material_retry_analyze 
        POST 요청 시 데코레이터에 의해 차단되는지 검증
        """
        pdf_material = StudyMaterial.objects.create(
            exam=self.exam,
            title="PDF 자료",
            material_type=MaterialType.PDF,
            status=MaterialStatus.COMPLETED,
            extracted_text="PDF 내용",
        )

        routes = [
            reverse("exams:material_extract", args=[pdf_material.id]),
            reverse("exams:material_analyze", args=[pdf_material.id]),
            reverse("exams:material_retry_analyze", args=[pdf_material.id]),
        ]

        for url in routes:
            response = self.client.post(url)
            self.assertEqual(
                response.status_code, 302,
                f"AI 경로가 차단되지 않음: {url}"
            )
            self.assertRedirects(response, reverse("exams:period_detail", args=[self.period.id]))

    # ============================================================
    # 계획이 없는 경우 - 기존 동작 유지
    # ============================================================

    def test_subject_update_allowed_when_plan_does_not_exist(self):
        """계획이 없는 시험기간의 과목은 기존처럼 수정할 수 있다."""
        self.daily_plan.delete()

        url = reverse("exams:subject_update", args=[self.period.id, self.exam.id])

        response = self.client.post(
            url,
            {
                "subject_name": "수정된 알고리즘",
                "exam_date": "2026-05-06",
                "priority": PriorityLevel.HIGH,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.exam.refresh_from_db()
        self.assertEqual(self.exam.subject_name, "수정된 알고리즘")

    def test_subject_delete_allowed_when_plan_does_not_exist(self):
        """계획이 없는 시험기간의 과목은 기존처럼 삭제할 수 있다."""
        self.daily_plan.delete()
        exam_id = self.exam.id

        url = reverse("exams:subject_delete", args=[self.period.id, exam_id])

        response = self.client.post(url)

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Exam.objects.filter(id=exam_id).exists())

    def test_material_delete_allowed_when_plan_does_not_exist(self):
        """계획이 없는 시험기간의 학습자료는 기존처럼 삭제할 수 있다."""
        self.daily_plan.delete()
        material_id = self.material.id

        url = reverse("exams:material_delete", args=[material_id])

        response = self.client.post(url)

        self.assertEqual(response.status_code, 302)
        self.assertFalse(StudyMaterial.objects.filter(id=material_id).exists())

class ExamPeriodCompletionTests(TestCase):
    """시험기간 수동/자동(Lazy Check) 종료 기능 검증"""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='tester@example.com', email='tester@example.com', password='pass1234!'
        )
        self.client.force_login(self.user)
        self.today = timezone.localdate()

    def test_manual_period_complete_success(self):
        """'시험기간 종료' POST 요청 시 status가 COMPLETED로 전환되는지 검증"""
        period = ExamPeriod.objects.create(
            user=self.user,
            title='2026 1학기 중간고사',
            start_date=self.today - datetime.timedelta(days=5),
            end_date=self.today + datetime.timedelta(days=5),
            status=ExamPeriodStatus.ACTIVE,
        )

        url = reverse('exams:period_complete', args=[period.id])
        response = self.client.post(url)

        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('exams:period_list'))

        period.refresh_from_db()
        self.assertEqual(period.status, ExamPeriodStatus.COMPLETED)

    def test_lazy_check_auto_completes_expired_period_on_detail_view(self):
        """end_date가 지난 ACTIVE 시험기간을 조회하면 자동으로 COMPLETED 전환되는지 검증"""
        expired_period = ExamPeriod.objects.create(
            user=self.user,
            title='지난 시험고사',
            start_date=self.today - datetime.timedelta(days=10),
            end_date=self.today - datetime.timedelta(days=1),
            status=ExamPeriodStatus.ACTIVE,
        )

        url = reverse('exams:period_detail', args=[expired_period.id])
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        expired_period.refresh_from_db()
        self.assertEqual(expired_period.status, ExamPeriodStatus.COMPLETED)

    def test_new_period_creation_allowed_after_previous_period_completed(self):
        """기존 시험기간이 COMPLETED 상태가 되면 새로운 ACTIVE 시험기간을 생성할 수 있는지 검증"""
        ExamPeriod.objects.create(
            user=self.user,
            title='이전 시험',
            start_date=self.today - datetime.timedelta(days=20),
            end_date=self.today - datetime.timedelta(days=10),
            status=ExamPeriodStatus.COMPLETED,
        )

        url = reverse('exams:period_create')
        post_data = {
            'title': '새 시험기간',
            'start_date': str(self.today),
            'end_date': str(self.today + datetime.timedelta(days=10)),
        }
        response = self.client.post(url, post_data)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            ExamPeriod.objects.filter(user=self.user, title='새 시험기간', status=ExamPeriodStatus.ACTIVE).exists()
        )

    def test_new_period_creation_rejected_when_active_period_exists(self):
        ExamPeriod.objects.create(
            user=self.user,
            title='진행 중인 시험',
            start_date=self.today,
            end_date=self.today + datetime.timedelta(days=10),
            status=ExamPeriodStatus.ACTIVE,
        )

        url = reverse('exams:period_create')
        post_data = {
            'title': '새 시험기간',
            'start_date': str(self.today),
            'end_date': str(self.today + datetime.timedelta(days=10)),
        }

        response = self.client.post(url, post_data)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            ExamPeriod.objects.filter(user=self.user, title='새 시험기간').exists()
        )

    def test_period_create_lazy_expires_active_before_check(self):
        expired_period = ExamPeriod.objects.create(
            user=self.user,
            title="지난 시험기간",
            start_date=timezone.localdate() - datetime.timedelta(days=30),
            end_date=timezone.localdate() - datetime.timedelta(days=1),
            status=ExamPeriodStatus.ACTIVE,
        )

        response = self.client.post(reverse('exams:period_create'), {
            'title': '새 시험기간',
            'start_date': timezone.localdate(),
            'end_date': timezone.localdate() + datetime.timedelta(days=14),
        })

        expired_period.refresh_from_db()
        self.assertEqual(expired_period.status, ExamPeriodStatus.COMPLETED)

        self.assertTrue(
            ExamPeriod.objects.filter(
                user=self.user, status=ExamPeriodStatus.ACTIVE
            ).exclude(id=expired_period.id).exists()
        )
        self.assertEqual(response.status_code, 302)

    def _available_time_formset_post_data(self, available_time, new_hours, new_minutes):
        return {
            'form-TOTAL_FORMS': '1',
            'form-INITIAL_FORMS': '1',
            'form-MIN_NUM_FORMS': '0',
            'form-MAX_NUM_FORMS': '1000',
            'form-0-id': str(available_time.id),
            'form-0-date': available_time.date.isoformat(),  # disabled 필드라 무시되지만 형식상 포함
            'form-0-hours': str(new_hours),
            'form-0-minutes': str(new_minutes),
        }


    def test_period_manage_available_time_blocked_when_completed_with_plan(self):
        period = ExamPeriod.objects.create(
            user=self.user,
            title="종료된 시험기간",
            start_date=timezone.localdate() - datetime.timedelta(days=10),
            end_date=timezone.localdate() + datetime.timedelta(days=5),
            status=ExamPeriodStatus.COMPLETED,
        )
        Exam.objects.create(
            exam_period=period,
            subject_name="수학",
            exam_date=period.end_date,
        )
        at = AvailableTime.objects.create(
            exam_period=period, date=timezone.localdate(), available_minutes=60,
        )
        DailyPlan.objects.create(
            exam_period=period, date=timezone.localdate(), available_minutes=60, planned_minutes=0,
        )

        response = self.client.post(
            reverse('exams:period_manage_available_time', args=[period.id]),
            self._available_time_formset_post_data(at, new_hours=2, new_minutes=0),
        )

        at.refresh_from_db()
        self.assertEqual(at.available_minutes, 60)
        self.assertRedirects(response, reverse('exams:period_manage', args=[period.id]))



    def test_period_update_blocked_when_completed_without_plan(self):
        period = ExamPeriod.objects.create(
            user=self.user,
            title="계획 없이 종료된 시험기간",
            start_date=timezone.localdate() - datetime.timedelta(days=10),
            end_date=timezone.localdate() + datetime.timedelta(days=5),
            status=ExamPeriodStatus.COMPLETED,
        )

        response = self.client.post(
            reverse('exams:period_update', args=[period.id]),
            {
                'title': '수정 시도',
                'start_date': period.start_date,
                'end_date': period.end_date,
            },
        )

        period.refresh_from_db()
        self.assertEqual(period.title, "계획 없이 종료된 시험기간")  # 변경되지 않음
        self.assertRedirects(response, reverse('exams:period_detail', args=[period.id]))


    def test_period_complete_preserves_related_records(self):
        period = ExamPeriod.objects.create(
            user=self.user,
            title="진행 중 시험기간",
            start_date=timezone.localdate() - datetime.timedelta(days=10),
            end_date=timezone.localdate() + datetime.timedelta(days=5),
            status=ExamPeriodStatus.ACTIVE,
        )
        exam = Exam.objects.create(
            exam_period=period,
            subject_name="영어",
            exam_date=period.end_date,
        )
        material = StudyMaterial.objects.create(
            exam=exam, material_type=MaterialType.TEXT, status=MaterialStatus.COMPLETED,
        )
        task = StudyTask.objects.create(exam=exam, title="영단어 암기")
        plan = DailyPlan.objects.create(
            exam_period=period, date=timezone.localdate(), available_minutes=60, planned_minutes=0,
        )

        self.client.post(reverse('exams:period_complete', args=[period.id]))

        period.refresh_from_db()
        self.assertEqual(period.status, ExamPeriodStatus.COMPLETED)
        self.assertTrue(Exam.objects.filter(id=exam.id).exists())
        self.assertTrue(StudyMaterial.objects.filter(id=material.id).exists())
        self.assertTrue(StudyTask.objects.filter(id=task.id).exists())
        self.assertTrue(DailyPlan.objects.filter(id=plan.id).exists())

    def test_period_complete_blocked_while_ai_analysis_processing(self):
        period = ExamPeriod.objects.create(
            user=self.user,
            title="AI 분석 중 시험기간",
            start_date=timezone.localdate() - datetime.timedelta(days=10),
            end_date=timezone.localdate() + datetime.timedelta(days=5),
            status=ExamPeriodStatus.ACTIVE,
        )
        exam = Exam.objects.create(exam_period=period, subject_name="수학", exam_date=period.end_date)
        material = StudyMaterial.objects.create(
            exam=exam,
            material_type=MaterialType.TEXT,
            status=MaterialStatus.COMPLETED,
            analysis_status=MaterialStatus.PROCESSING,
        )

        response = self.client.post(reverse('exams:period_complete', args=[period.id]))

        period.refresh_from_db()
        self.assertEqual(period.status, ExamPeriodStatus.ACTIVE)
        self.assertRedirects(response, reverse('exams:period_list'))


    def test_period_complete_blocked_while_pdf_extraction_processing(self):
        period = ExamPeriod.objects.create(
            user=self.user,
            title="PDF 추출 중 시험기간",
            start_date=timezone.localdate() - datetime.timedelta(days=10),
            end_date=timezone.localdate() + datetime.timedelta(days=5),
            status=ExamPeriodStatus.ACTIVE,
        )
        exam = Exam.objects.create(exam_period=period, subject_name="영어", exam_date=period.end_date)
        material = StudyMaterial.objects.create(
            exam=exam,
            material_type=MaterialType.PDF,
            status=MaterialStatus.PROCESSING,
        )

        response = self.client.post(reverse('exams:period_complete', args=[period.id]))

        period.refresh_from_db()
        self.assertEqual(period.status, ExamPeriodStatus.ACTIVE)
        self.assertRedirects(response, reverse('exams:period_list'))


    def test_lazy_check_skips_expired_period_with_processing_material(self):
        period = ExamPeriod.objects.create(
            user=self.user,
            title="만료됐지만 처리 중인 시험기간",
            start_date=timezone.localdate() - datetime.timedelta(days=30),
            end_date=timezone.localdate() - datetime.timedelta(days=1),
            status=ExamPeriodStatus.ACTIVE,
        )
        exam = Exam.objects.create(exam_period=period, subject_name="과학", exam_date=period.end_date)
        StudyMaterial.objects.create(
            exam=exam,
            material_type=MaterialType.PDF,
            status=MaterialStatus.PROCESSING,
        )

        self.client.get(reverse('exams:period_list'))

        period.refresh_from_db()
        self.assertEqual(period.status, ExamPeriodStatus.ACTIVE)

    def test_lazy_check_skips_expired_period_with_processing_material_on_detail_view(self):
        """period_detail() GET 조회 시에도 PROCESSING 중인 학습자료가 있는 만료
        ACTIVE 시험기간은 COMPLETED로 전환되지 않고 ACTIVE로 유지되어야 한다.

        (회귀 방지: _get_owned_exam_period()가 한때 중복 정의되어, 뒤에 정의된
        non-lock 버전이 앞의 lock 기반 버전을 덮어쓰는 바람에 이 케이스가
        검증되지 않고 있었다)
        """
        period = ExamPeriod.objects.create(
            user=self.user,
            title="만료됐지만 처리 중인 시험기간",
            start_date=timezone.localdate() - datetime.timedelta(days=30),
            end_date=timezone.localdate() - datetime.timedelta(days=1),
            status=ExamPeriodStatus.ACTIVE,
        )
        exam = Exam.objects.create(exam_period=period, subject_name="과학", exam_date=period.end_date)
        StudyMaterial.objects.create(
            exam=exam,
            material_type=MaterialType.PDF,
            status=MaterialStatus.PROCESSING,
        )

        response = self.client.get(reverse('exams:period_detail', args=[period.id]))

        self.assertEqual(response.status_code, 200)
        period.refresh_from_db()
        self.assertEqual(period.status, ExamPeriodStatus.ACTIVE)
        
class ConcurrencyDefenseAndRaceConditionTests(TestCase):
    """AI 분석/추출 간 경쟁 상태 및 처리 중 자료 삭제 방어 검증"""

    def setUp(self):
        self.user = User.objects.create_user(
            username="concurrency_tester@example.com",
            email="concurrency_tester@example.com",
            password="pass1234!",
        )
        self.client.force_login(self.user)
        self.period = ExamPeriod.objects.create(
            user=self.user,
            title="동시성 테스트 기간",
            start_date=datetime.date(2026, 8, 1),
            end_date=datetime.date(2026, 8, 20),
        )
        self.exam = Exam.objects.create(
            exam_period=self.period,
            subject_name="운영체제",
            exam_date=datetime.date(2026, 8, 15),
        )
        self.material = StudyMaterial.objects.create(
            exam=self.exam,
            title="동시성 테스트 자료",
            material_type=MaterialType.TEXT,
            extracted_text="프로세스 스케줄링 내용",
            status=MaterialStatus.COMPLETED,
            analysis_status=MaterialStatus.PENDING,
        )

    def test_material_delete_blocked_when_processing(self):
        """처리 중인 학습자료는 삭제할 수 없다."""
        self.material.status = MaterialStatus.PROCESSING
        self.material.save(update_fields=["status"])

        url = reverse("exams:material_delete", args=[self.material.id])
        response = self.client.post(url, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(StudyMaterial.objects.filter(id=self.material.id).exists())
        messages_list = list(response.context['messages'])
        self.assertTrue(any("현재 처리 중인" in str(m) for m in messages_list))

    def test_material_delete_blocked_when_analysis_processing(self):
        """AI 분석 중인 학습자료는 삭제할 수 없다."""
        self.material.analysis_status = MaterialStatus.PROCESSING
        self.material.save(update_fields=["analysis_status"])

        url = reverse("exams:material_delete", args=[self.material.id])
        response = self.client.post(url, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(StudyMaterial.objects.filter(id=self.material.id).exists())
        messages_list = list(response.context['messages'])
        self.assertTrue(any("현재 처리 중인" in str(m) for m in messages_list))

    def test_material_analyze_blocked_when_extraction_not_completed(self):
        """추출이 완료되지 않은 상태에서는 AI 분석을 시작할 수 없다."""
        self.material.status = MaterialStatus.PROCESSING
        self.material.save(update_fields=["status"])

        url = reverse("exams:material_analyze", args=[self.material.id])
        response = self.client.post(url, follow=True)

        self.material.refresh_from_db()
        self.assertNotEqual(self.material.analysis_status, MaterialStatus.PROCESSING)
        messages_list = list(response.context['messages'])
        self.assertTrue(any("텍스트 추출이 완료된 자료만" in str(m) or "추출이 진행 중" in str(m) for m in messages_list))
