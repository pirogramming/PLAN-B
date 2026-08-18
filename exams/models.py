from django.conf import settings
from django.db import models
from django.core.exceptions import ValidationError
from django.utils import timezone

from core.choices import (
    ExamPeriodStatus,
    MaterialStatus,
    MaterialType,
    PriorityLevel,
    TaskDepth,
    TaskDifficulty,
    TaskType,
)

DEFAULT_SAFE_EXTRACTION_ERROR_MESSAGE = "추출에 실패했습니다."

# =====================================================================

class ExamPeriod(models.Model):
    """
    시험기간 전체 정보를 관리
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='exam_periods',
        verbose_name="사용자"
    )
    title = models.CharField(max_length=100, verbose_name="시험기간명")  # 예: "2026 2학기 중간고사"
    start_date = models.DateField(verbose_name="시작일")
    end_date = models.DateField(verbose_name="종료일")
    status = models.CharField(
        max_length=20,
        choices=ExamPeriodStatus.choices,
        default=ExamPeriodStatus.DRAFT,
        verbose_name="시험기간 상태"
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="생성일시")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="수정일시")

    class Meta:
        db_table = 'exam_periods'
        verbose_name = '시험기간'
        verbose_name_plural = '시험기간 목록'
        ordering = ['-start_date']

    def __str__(self):
        return f"[{self.user}] {self.title} ({self.status})"

    @property
    def progress_percent(self):
        """
        오늘까지의 날짜 경과율 (사이드바 진행률 바 용도).
        시작 전은 0%, 종료 후는 100%로 고정한다.
        """
        today = timezone.localdate()
        if today < self.start_date:
            return 0

        total_days = (self.end_date - self.start_date).days
        if total_days <= 0:
            return 100
        if today >= self.end_date:
            return 100

        elapsed_days = (today - self.start_date).days
        return round(elapsed_days / total_days * 100)


class AvailableTime(models.Model):
    """
    시험기간 내 날짜별 전체 공부 가능시간
    """
    exam_period = models.ForeignKey(
        ExamPeriod,
        on_delete=models.CASCADE,
        related_name='available_times',
        verbose_name="시험기간"
    )
    date = models.DateField(verbose_name="날짜")
    available_minutes = models.PositiveIntegerField(default=0, verbose_name="공부 가능시간(분)")

    class Meta:
        db_table = 'available_times'
        verbose_name = '날짜별 가용시간'
        verbose_name_plural = '날짜별 가용시간 목록'
        unique_together = ('exam_period', 'date')
        ordering = ['date']

    def __str__(self):
        return f"{self.date}: {self.available_minutes}분"


class Exam(models.Model):
    """
    개별 시험 과목 모델
    """
    exam_period = models.ForeignKey(
        ExamPeriod,
        on_delete=models.CASCADE,
        related_name='exams',
        verbose_name="시험기간"
    )
    subject_name = models.CharField(max_length=100, verbose_name="과목명")
    exam_date = models.DateField(verbose_name="시험일")
    priority = models.CharField(
        max_length=10,
        choices=PriorityLevel.choices,
        default=PriorityLevel.MEDIUM,
        verbose_name="과목 우선순위"
    )
    speed_factor = models.FloatField(default=1.0, verbose_name="공부 속도 보정계수")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="생성일시")

    class Meta:
        db_table = 'exams'
        verbose_name = '시험 과목'
        verbose_name_plural = '시험 과목 목록'
        ordering = ['exam_date']

    def __str__(self):
        return f"{self.subject_name} ({self.exam_date})"


class StudyMaterial(models.Model):
    """
    과목별 시험 범위 학습 자료
    """
    exam = models.ForeignKey(
        Exam,
        on_delete=models.CASCADE,
        related_name='study_materials',
        verbose_name="시험 과목"
    )
    title = models.CharField(max_length=150, verbose_name="자료/범위명")
    material_type = models.CharField(
        max_length=10,
        choices=MaterialType.choices,
        default=MaterialType.TEXT,
        verbose_name="자료 입력 유형"
    )
    file = models.FileField(upload_to='materials/%Y/%m/', null=True, blank=True, verbose_name="첨부 파일(PDF)")
    extracted_text = models.TextField(null=True, blank=True, verbose_name="추출된 텍스트 내용")
    status = models.CharField(
        max_length=20,
        choices=MaterialStatus.choices,
        default=MaterialStatus.PENDING,
        verbose_name="텍스트 추출 상태"
    )
    error_message = models.TextField(null=True, blank=True, verbose_name="추출/파싱 실패 원인")
    user_error_message = models.TextField(null=True, blank=True, verbose_name="추출 실패 시 사용자 노출용 메시지")
    extraction_started_at = models.DateTimeField(null=True, blank=True, verbose_name="PDF 추출 시작 시각")
    extraction_run_id = models.UUIDField(null=True, blank=True, verbose_name="PDF 추출 실행 식별자")
 
    # 리뷰 확정 사항: 텍스트 추출 성공 여부와 AI 분석 성공 여부는 서로 다른 단계라 분리한다.
    analysis_status = models.CharField(
        max_length=20,
        choices=MaterialStatus.choices,
        default=MaterialStatus.PENDING,
        verbose_name="AI 분석 상태"
    )
    analysis_error_message = models.TextField(null=True, blank=True, verbose_name="AI 분석 실패 사유")
    analysis_retry_count = models.PositiveSmallIntegerField(default=0, verbose_name="AI 분석 사용자 재시도 횟수")
    analysis_started_at = models.DateTimeField(null=True, blank=True, verbose_name="AI 분석 시작 시각")
    analysis_run_id = models.UUIDField(null=True, blank=True, verbose_name="AI 분석 실행 식별자")
 
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="생성일시")
 
    class Meta:
        db_table = 'study_materials'
        verbose_name = '학습 자료'
        verbose_name_plural = '학습 자료 목록'
 
    def __str__(self):
        return f"[{self.exam.subject_name}] {self.title}"
 
    def get_display_error_message(self):
        """
        화면/API에 노출할 안전한 추출 실패 메시지를 반환한다.
 
        user_error_message가 비어 있어도 error_message(PDFium 원본 오류, storage
        내부 정보 등 내부 상세 원인을 포함할 수 있음)로 폴백하지 않는다.
        user_error_message 필드 도입 이전에 저장된 기존 데이터의 경우에도
        여기서 걸러지도록 하기 위함이다.
        """
        if self.user_error_message:
            return self.user_error_message
        if self.status == MaterialStatus.FAILED:
            return DEFAULT_SAFE_EXTRACTION_ERROR_MESSAGE
        return None


class StudyTask(models.Model):
    """
    하루 학습 분량
    """
    exam = models.ForeignKey(
        Exam,
        on_delete=models.CASCADE,
        related_name='study_tasks',
        verbose_name="시험 과목"
    )
    study_material = models.ForeignKey(
        StudyMaterial,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='study_tasks',
        verbose_name="관련 학습 자료"
    )
    unit_name = models.CharField(max_length=100, null=True, blank=True, verbose_name="단원명")
    title = models.CharField(max_length=200, verbose_name="학습 작업명")
    task_type = models.CharField(
        max_length=20,
        choices=TaskType.choices,
        default=TaskType.CONCEPT,
        verbose_name="작업 유형"
    )
    importance = models.CharField(
        max_length=10,
        choices=PriorityLevel.choices,
        default=PriorityLevel.MEDIUM,
        verbose_name="중요도"
    )
    depth = models.CharField(
        max_length=10,
        choices=TaskDepth.choices,
        default=TaskDepth.BASIC,
        verbose_name="학습 깊이"
    )
    difficulty = models.CharField(
        max_length=10,
        choices=TaskDifficulty.choices,
        default=TaskDifficulty.NORMAL,
        verbose_name="난이도"
    )
    estimated_min_minutes = models.PositiveIntegerField(default=0, verbose_name="최소 예상시간(분)")
    estimated_max_minutes = models.PositiveIntegerField(default=0, verbose_name="최대 예상시간(분)")
    ai_reason = models.TextField(null=True, blank=True, verbose_name="AI 추천 이유")
    source_pages = models.JSONField(default=list, blank=True, verbose_name="근거 PDF 페이지 목록")
    is_user_modified = models.BooleanField(default=False, verbose_name="사용자 직접 수정 여부")
    is_confirmed = models.BooleanField(default=False, verbose_name="작업 확정 여부")
    order = models.PositiveIntegerField(default=1, verbose_name="작업 순서")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="생성일시")

    class Meta:
        db_table = 'study_tasks'
        verbose_name = '학습 작업'
        verbose_name_plural = '학습 작업 목록'
        ordering = ['order', 'id']

    def clean(self):
        super().clean()
        if self.study_material and self.study_material.exam_id != self.exam_id:
            raise ValidationError({
                'study_material': '선택한 학습 자료의 과목과 해당 학습 작업의 과목이 일치하지 않습니다.'
            })
        if self.estimated_min_minutes > self.estimated_max_minutes:
            raise ValidationError({
                'estimated_min_minutes': '최소 예상시간은 최대 예상시간보다 클 수 없습니다.'
            })

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"[{self.exam.subject_name}] {self.title}"