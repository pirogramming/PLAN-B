import uuid

from django.db import models
from django.core.exceptions import ValidationError
from core.choices import (
    DailyPlanStatus,
    ProgressStatus,
    RecoveryActionType,
    RecoveryPlanStatus,
    RecoveryType,
)


class DailyPlan(models.Model):
    exam_period = models.ForeignKey(
        'exams.ExamPeriod',
        on_delete=models.CASCADE,
        related_name='daily_plans',
    )
    date = models.DateField()
    available_minutes = models.PositiveIntegerField()
    planned_minutes = models.PositiveIntegerField()
    status = models.CharField(
        max_length=20,
        choices=DailyPlanStatus.choices,
        default=DailyPlanStatus.PLANNED,
    )
    # 마감 여부는 status와 별개로 관리 (status는 진행 기록 입력 때마다 갱신되므로
    # "마감됨"이라는 1회성 이벤트를 별도 필드로 분리)
    finalized_at = models.DateTimeField(null=True, blank=True)


    class Meta:
        ordering = ['date']
        constraints = [
            models.UniqueConstraint(
                fields=['exam_period', 'date'],
                name='unique_daily_plan_per_exam_period_date',
            )
        ]

    def __str__(self):
        return f'{self.exam_period} - {self.date}'


class DailyPlanItem(models.Model):
    daily_plan = models.ForeignKey(
        DailyPlan,
        on_delete=models.CASCADE,
        related_name='items',
    )
    study_task = models.ForeignKey(
        'exams.StudyTask',
        on_delete=models.PROTECT,
        related_name='plan_items',
    )
    # 생성 당시 값을 별도 저장 (StudyTask 예상시간이 나중에 바뀌어도 과거 기록 불변)
    planned_minutes = models.PositiveIntegerField()
    order = models.PositiveIntegerField()
    status = models.CharField(
        max_length=20,
        choices=DailyPlanStatus.choices,
        default=DailyPlanStatus.PLANNED,
    )

    class Meta:
        ordering = ['order']

    def __str__(self):
        return f'{self.daily_plan} - {self.study_task}'


class ProgressLog(models.Model):
    """
    작업당 하루 한 번 결과를 입력하는 MVP 정책에 맞춰 OneToOne으로 관리.
    수정 시에는 record_progress 서비스에서 update_or_create로 덮어씀.
    """
    daily_plan_item = models.OneToOneField(
        DailyPlanItem,
        on_delete=models.CASCADE,
        related_name='progress_log',
    )
    progress_status = models.CharField(
        max_length=20, choices=ProgressStatus.choices
    )
    actual_minutes = models.PositiveIntegerField(null=True, blank=True)
    # 완료=100, 못함=0, 일부완료=1~99. 속도보정은 actual_minutes, 남은 작업량 계산은 completion_percent로 책임 분리.
    completion_percent = models.PositiveSmallIntegerField(null=True, blank=True)
    recorded_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.daily_plan_item} - {self.progress_status}'


class RecoveryPlan(models.Model):
    exam_period = models.ForeignKey(
        'exams.ExamPeriod',
        on_delete=models.CASCADE,
        related_name='recovery_plans',
    )
    source_daily_plan = models.ForeignKey(
        'planner.DailyPlan',
        on_delete=models.CASCADE,
        related_name='recovery_plans',
    )
    # 같은 계산 시점에 생성된 분량유지형/핵심집중형 두 복구안을 묶어서 비교하기 위한 그룹 키
    recovery_group_id = models.UUIDField(default=uuid.uuid4, db_index=True)
    recovery_type = models.CharField(
        max_length=20, choices=RecoveryType.choices
    )
    status = models.CharField(
        max_length=20,
        choices=RecoveryPlanStatus.choices,
        default=RecoveryPlanStatus.PENDING,
    )
    summary = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['recovery_group_id'],
                condition=models.Q(status=RecoveryPlanStatus.APPLIED),
                name='unique_applied_recovery_plan_per_group',
            )
        ]


    def clean(self):
        super().clean()

        if not self.exam_period_id or not self.source_daily_plan_id:
            return

        if self.exam_period_id != self.source_daily_plan.exam_period_id:
            raise ValidationError({
                'source_daily_plan': (
                    'source_daily_plan의 exam_period와 '
                    'RecoveryPlan의 exam_period가 일치해야 합니다.'
                )
            })

    def __str__(self):
        return f'{self.exam_period} - {self.recovery_type}'


class RecoveryPlanItem(models.Model):
    recovery_plan = models.ForeignKey(
        RecoveryPlan,
        on_delete=models.CASCADE,
        related_name='items',
    )
    study_task = models.ForeignKey(
        'exams.StudyTask',
        on_delete=models.PROTECT,
        related_name='recovery_items',
    )
    original_date = models.DateField(null=True, blank=True)
    changed_date = models.DateField(null=True, blank=True)
    action_type = models.CharField(
        max_length=20, choices=RecoveryActionType.choices
    )
    reason = models.TextField(null=True, blank=True)
    remaining_minutes = models.PositiveIntegerField(default=0)

    source_daily_plan_item = models.ForeignKey(
        'DailyPlanItem',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='recovery_source_items',
        help_text=(
            "이 복구 항목이 원래 있던 DailyPlanItem. "
            "None이면 실패한 작업을 새로 배치하는 것이고, "
            "값이 있으면 학습 순서 보존을 위해 기존에 배치돼 있던 "
            "미래 작업을 이동시키는 것이다."
        ),
    )

    is_carry_along = models.BooleanField(
        default=False,
        help_text=(
            "학습 순서 보존을 위해 함께 재배치/제외되는 후속 작업이면 True. "
            "source_daily_plan_item은 원본이 삭제되면 SET_NULL로 None이 되므로, "
            "carry-along 여부 자체는 이 필드로 별도 보존한다 (FK 값만으로는 "
            "'원래 실패 작업'과 '원본이 사라진 carry-along'을 구분할 수 없다)."
        ),
    )
    
    def __str__(self):
        return f'{self.recovery_plan} - {self.study_task} ({self.action_type})'
