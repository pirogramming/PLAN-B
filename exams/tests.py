import datetime
import io
from unittest.mock import patch

from django.test import TestCase
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

from django.core.files.uploadedfile import SimpleUploadedFile
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


class PdfExtractorTestCase(TestCase):
    
    def test_extract_text_from_invalid_pdf(self):
        """손상되거나 PDF가 아닌 일반 텍스트 파일 입력 시 PdfExtractionError 발생 검증"""
        # 1. 가짜 텍스트 파일 준비
        fake_pdf_content = b"This is not a real PDF content."
        dummy_file = SimpleUploadedFile("test.pdf", fake_pdf_content, content_type="application/pdf")

        # 2. 예외 발생 여부 테스트
        with self.assertRaises(PdfExtractionError) as context:
            extract_text_from_pdf(dummy_file)

        self.assertIn("올바른 PDF 형식이 아니거나 손상된 파일입니다", str(context.exception))

    def test_extract_text_from_empty_pdf_or_image(self):
        """텍스트 레이어가 없는 빈/스캔 PDF일 때 예외 처리 검증"""
        # pypdf로 빈 1페이지짜리 PDF 동적 생성
        import pypdf
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=100, height=100)
        
        pdf_buffer = io.BytesIO()
        writer.write(pdf_buffer)
        pdf_buffer.seek(0)

        dummy_file = SimpleUploadedFile("blank.pdf", pdf_buffer.read(), content_type="application/pdf")

        with self.assertRaises(PdfExtractionError) as context:
            extract_text_from_pdf(dummy_file)

        self.assertIn("PDF에서 텍스트를 추출할 수 없습니다", str(context.exception))