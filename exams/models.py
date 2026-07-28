from django.conf import settings
from django.db import models


class ExamPeriodStatus(models.TextChoices):
    DRAFT = 'draft', '작성 중'
    ACTIVE = 'active', '진행 중'
    COMPLETED = 'completed', '완료'
    ARCHIVED = 'archived', '보관'


class ImportanceLevel(models.TextChoices):
    HIGH = 'high', '높음'
    MEDIUM = 'medium', '보통'
    LOW = 'low', '낮음'


class TaskDepth(models.TextChoices):
    CORE = 'core', '핵심'
    BASIC = 'basic', '기본'
    OPTIONAL = 'optional', '선택'


class TaskDifficulty(models.TextChoices):
    EASY = 'easy', '쉬움'
    NORMAL = 'normal', '보통'
    HARD = 'hard', '어려움'


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
    name = models.CharField(max_length=100, verbose_name="과목명")
    exam_date = models.DateField(verbose_name="시험일")
    importance = models.CharField(
        max_length=10,
        choices=ImportanceLevel.choices,
        default=ImportanceLevel.MEDIUM,
        verbose_name="우선순위"
    )
    speed_factor = models.FloatField(default=1.0, verbose_name="공부 속도 보정계수")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="생성일시")

    class Meta:
        db_table = 'exams'
        verbose_name = '시험 과목'
        verbose_name_plural = '시험 과목 목록'
        ordering = ['exam_date']

    def __str__(self):
        return f"{self.name} ({self.exam_date})"


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
    file = models.FileField(upload_to='materials/%Y/%m/', null=True, blank=True, verbose_name="첨부 파일(PDF)")
    extracted_text = models.TextField(null=True, blank=True, verbose_name="추출된 텍스트 내용") #pdf에서 추출하거나 아니면 사용자가 직접 작성 
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="생성일시")

    class Meta:
        db_table = 'study_materials'
        verbose_name = '학습 자료'
        verbose_name_plural = '학습 자료 목록'

    def __str__(self):
        return f"[{self.exam.name}] {self.title}"


class StudyTask(models.Model):
    """
    학습 작업 
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
    task_type = models.CharField(max_length=50, null=True, blank=True, verbose_name="작업 유형")
    

    importance = models.CharField(
        max_length=10,
        choices=ImportanceLevel.choices,
        default=ImportanceLevel.MEDIUM,
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
    
    is_confirmed = models.BooleanField(default=False, verbose_name="작업 확정 여부")
    order = models.PositiveIntegerField(default=1, verbose_name="작업 순서")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="생성일시")

    class Meta:
        db_table = 'study_tasks'
        verbose_name = '학습 작업'
        verbose_name_plural = '학습 작업 목록'
        ordering = ['order', 'id']

    def __str__(self):
        return f"[{self.exam.name}] {self.title}"