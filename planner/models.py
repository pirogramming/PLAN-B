from django.db import models

# Create your models here.
from django.db import models

from core.choices import (
    DailyPlanStatus,
    ProgressStatus,
    RecoveryActionType,
    RecoveryPlanStatus,
    RecoveryType,
)
from exams.models import Exam, StudyTask


class DailyPlan(models.Model):
    exam = models.ForeignKey(
        Exam,
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

    class Meta:
        ordering = ['date']
        constraints = [
            models.UniqueConstraint(
                fields=['exam', 'date'],
                name='unique_exam_daily_plan_date',
            )
        ]

    def __str__(self):
        return f'{self.exam} - {self.date}'


class DailyPlanItem(models.Model):
    daily_plan = models.ForeignKey(
        DailyPlan,
        on_delete=models.CASCADE,
        related_name='items',
    )
    study_task = models.ForeignKey(
        StudyTask,
        on_delete=models.CASCADE,
        related_name='plan_items',
    )
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
    daily_plan_item = models.ForeignKey(
        DailyPlanItem,
        on_delete=models.CASCADE,
        related_name='progress_logs',
    )
    progress_status = models.CharField(
        max_length=20, choices=ProgressStatus.choices
    )
    actual_minutes = models.PositiveIntegerField(null=True, blank=True)
    completed_amount = models.PositiveIntegerField(null=True, blank=True)
    recorded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.daily_plan_item} - {self.progress_status}'


class RecoveryPlan(models.Model):
    exam = models.ForeignKey(
        Exam,
        on_delete=models.CASCADE,
        related_name='recovery_plans',
    )
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

    def __str__(self):
        return f'{self.exam} - {self.recovery_type}'


class RecoveryPlanItem(models.Model):
    recovery_plan = models.ForeignKey(
        RecoveryPlan,
        on_delete=models.CASCADE,
        related_name='items',
    )
    study_task = models.ForeignKey(
        StudyTask,
        on_delete=models.CASCADE,
        related_name='recovery_items',
    )
    original_date = models.DateField(null=True, blank=True)
    changed_date = models.DateField(null=True, blank=True)
    action_type = models.CharField(
        max_length=20, choices=RecoveryActionType.choices
    )
    reason = models.TextField(null=True, blank=True)

    def __str__(self):
        return f'{self.recovery_plan} - {self.study_task} ({self.action_type})'