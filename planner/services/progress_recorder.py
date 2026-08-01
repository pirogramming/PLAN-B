"""
학습 진행 결과(ProgressLog)를 저장하고, 관련 상태(DailyPlanItem, DailyPlan)와
speed_factor를 갱신하는 모듈. 하루 마감(finalize_daily_plan) 시
복구 필요 여부를 판단하고 recovery.py에 복구안 생성을 위임한다.

책임 범위:
    ProgressLog 생성/수정 -> DailyPlanItem 상태 동기화
    -> speed_factor 재계산 -> DailyPlan 상태 재계산
    (마감 시) 미완료 항목 판단 -> recovery.generate_recovery_options() 호출
"""

from django.db import transaction
from django.utils import timezone
from planner.services.recovery import generate_recovery_options
from core.choices import DailyPlanStatus, ProgressStatus
from planner.models import ProgressLog, DailyPlan
from planner.services.speed_calibrator import recalculate_speed_factor

PROGRESS_TO_ITEM_STATUS = {
    ProgressStatus.DONE: DailyPlanStatus.COMPLETED,
    ProgressStatus.PARTIAL: DailyPlanStatus.AT_RISK,
    ProgressStatus.NOT_DONE: DailyPlanStatus.AT_RISK,
}


def normalize_completion_percent(status, completion_percent):
    """
    status에 따라 completion_percent 값을 검증/보정한다.

    - done: 서버에서 100으로 고정
    - not_done: 서버에서 0으로 고정
    - partial: 1~99 범위 필수, 벗어나면 ValueError
    """
    if status == ProgressStatus.DONE:
        return 100
    if status == ProgressStatus.NOT_DONE:
        return 0
    if status == ProgressStatus.PARTIAL:
        if completion_percent is None or not (1 <= completion_percent <= 99):
            raise ValueError(
                "일부완료 상태에서는 completion_percent가 1~99 사이여야 합니다."
            )
        return completion_percent
    raise ValueError(f"알 수 없는 progress_status: {status}")


def normalize_actual_minutes(status, actual_minutes):
    """
    status에 따라 actual_minutes 값을 검증/보정한다.

    - done, partial: 필수, 0보다 커야 함
    - not_done: 입력값과 무관하게 항상 0으로 고정
    """
    if status in (ProgressStatus.DONE, ProgressStatus.PARTIAL):
        if actual_minutes is None or actual_minutes <= 0:
            raise ValueError(
                "완료/일부완료 상태에서는 actual_minutes가 0보다 커야 합니다."
            )
        return actual_minutes
    if status == ProgressStatus.NOT_DONE:
        return 0
    raise ValueError(f"알 수 없는 progress_status: {status}")


def determine_daily_plan_status(item_statuses: list[str]) -> str:
    """
    하루 계획에 속한 DailyPlanItem들의 status 목록을 받아
    DailyPlan 전체 상태를 결정하는 순수 함수.

    규칙:
        항목이 없으면 -> planned
        전부 completed -> completed
        하나라도 at_risk -> at_risk
        일부만 completed, 나머지 planned -> in_progress
        그 외(전부 planned 등) -> planned
    """
    if not item_statuses:
        return DailyPlanStatus.PLANNED

    if all(s == DailyPlanStatus.COMPLETED for s in item_statuses):
        return DailyPlanStatus.COMPLETED

    if any(s == DailyPlanStatus.AT_RISK for s in item_statuses):
        return DailyPlanStatus.AT_RISK

    if any(s == DailyPlanStatus.COMPLETED for s in item_statuses):
        return DailyPlanStatus.IN_PROGRESS

    return DailyPlanStatus.PLANNED

class FinalizedDailyPlanEditError(Exception):
    pass

@transaction.atomic
def record_progress(
    *,
    daily_plan_item,
    status: str,
    actual_minutes: int | None,
    completion_percent: int | None = None,
):
    daily_plan = (
        DailyPlan.objects
        .select_for_update()
        .get(pk=daily_plan_item.daily_plan_id)
    )

    if daily_plan.finalized_at is not None:
        raise FinalizedDailyPlanEditError(
            "마감된 계획의 진행 기록은 수정할 수 없습니다."
        )

    normalized_percent = normalize_completion_percent(status, completion_percent)
    normalized_minutes = normalize_actual_minutes(status, actual_minutes)

    progress_log, _created = ProgressLog.objects.update_or_create(
        daily_plan_item=daily_plan_item,
        defaults={
            "progress_status": status,
            "actual_minutes": normalized_minutes,
            "completion_percent": normalized_percent,
        },
    )

    daily_plan_item.status = PROGRESS_TO_ITEM_STATUS[status]
    daily_plan_item.save(update_fields=["status"])

    exam = daily_plan_item.study_task.exam
    updated_speed_factor = recalculate_speed_factor(exam)

    item_statuses = list(
        daily_plan.items.values_list("status", flat=True)
    )
    daily_plan.status = determine_daily_plan_status(item_statuses)
    daily_plan.save(update_fields=["status"])

    return {
        "progress_log": progress_log,
        "daily_plan_item_status": daily_plan_item.status,
        "daily_plan_status": daily_plan.status,
        "updated_speed_factor": updated_speed_factor,
    }


class DailyPlanAlreadyFinalizedError(Exception):
    pass

class FutureDailyPlanFinalizeError(Exception):
    pass

class IncompleteProgressError(Exception):
    def __init__(self, unrecorded_items):
        self.unrecorded_items = unrecorded_items
        super().__init__(
            f"{len(unrecorded_items)}개 작업에 진행 기록이 입력되지 않았습니다."
        )


@transaction.atomic
def finalize_daily_plan(daily_plan) -> dict:
    """
    하루 계획을 마감한다.

    - 미래 날짜 계획은 마감 불가 (과거/오늘은 허용 — 마감을 놓친 날도
      나중에 복구할 수 있어야 하므로)
    - 조건부 UPDATE로 finalized_at을 원자적으로 선점해 중복 마감을 막는다
      (select_for_update만으로는 SQLite에서 실제 잠금이 걸리지 않으므로 병행)
    - 진행 기록(ProgressLog)이 없는 항목이 하나라도 있으면 IncompleteProgressError
    - PARTIAL/NOT_DONE 항목이 있으면 복구안(생성만, 미적용)까지 트랜잭션 안에서 생성
    - DailyPlanItem/ProgressLog는 건드리지 않고 과거 기록으로 보존
    """
    if daily_plan.date > timezone.localdate():
        raise FutureDailyPlanFinalizeError("미래 계획은 마감할 수 없습니다.")

    now = timezone.now()
    claimed = DailyPlan.objects.filter(
        pk=daily_plan.pk, finalized_at__isnull=True,
    ).update(finalized_at=now)

    if claimed == 0:
        already = DailyPlan.objects.get(pk=daily_plan.pk)
        raise DailyPlanAlreadyFinalizedError(
            f"{already}는 이미 {already.finalized_at}에 마감되었습니다."
        )

    locked_plan = (
        DailyPlan.objects
        .select_for_update()
        .get(pk=daily_plan.pk)
    )

    items = list(
        locked_plan.items.select_related('progress_log', 'study_task__exam')
    )

    unrecorded_items = [
        item for item in items if not hasattr(item, 'progress_log')
    ]
    if unrecorded_items:
        raise IncompleteProgressError(unrecorded_items)

    unfinished_items = [
        item for item in items
        if item.progress_log.progress_status in (
            ProgressStatus.PARTIAL, ProgressStatus.NOT_DONE
        )
    ]

    recovery_plans = None
    if unfinished_items:
        recovery_plans = generate_recovery_options(
            daily_plan=locked_plan,
            unfinished_items=unfinished_items,
        )

    return {
        'daily_plan': locked_plan,
        'needs_recovery': bool(unfinished_items),
        'unfinished_items': unfinished_items,
        'recovery_plans': recovery_plans,
    }