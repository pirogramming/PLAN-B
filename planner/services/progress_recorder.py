"""
학습 진행 결과(ProgressLog)를 저장하고, 관련 상태(DailyPlanItem, DailyPlan)와
speed_factor를 갱신하는 모듈.

책임 범위는 여기까지다:
    ProgressLog 생성/수정 -> DailyPlanItem 상태 동기화
    -> speed_factor 재계산 -> DailyPlan 상태 재계산

하루 마감 시 복구 필요 여부를 판단하는 로직(finalize_daily_plan)은
별도 PR/모듈에서 다룬다. 여기서 복구 여부를 판단하지 않는다.
"""
from django.db import transaction

from core.choices import DailyPlanStatus, ProgressStatus
from planner.models import ProgressLog
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
    - not_done: 없으면 0으로 보정
    """
    if status in (ProgressStatus.DONE, ProgressStatus.PARTIAL):
        if actual_minutes is None or actual_minutes <= 0:
            raise ValueError(
                "완료/일부완료 상태에서는 actual_minutes가 0보다 커야 합니다."
            )
        return actual_minutes
    return actual_minutes or 0


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


@transaction.atomic
def record_progress(
    *,
    daily_plan_item,
    status: str,
    actual_minutes: int | None,
    completion_percent: int | None = None,
):
    """
    진행 결과를 저장하고 관련 상태를 갱신한다.

    처리 순서:
        1. 입력값 검증/정규화
        2. ProgressLog update_or_create (재제출 시 갱신)
        3. DailyPlanItem.status 동기화
        4. 해당 과목 speed_factor 재계산
        5. DailyPlan 상태 재계산
    """
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

    daily_plan = daily_plan_item.daily_plan
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