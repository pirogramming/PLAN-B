"""
finalize_daily_plan()에서 넘겨받은 미완료 작업(PARTIAL/NOT_DONE)을 기준으로
분량유지형/핵심집중형 복구안 2종을 계산만 하고 저장한다.
DailyPlan/DailyPlanItem은 건드리지 않는다 (사용자 승인 전까지 미적용).
"""
import uuid

from django.db.models import Sum
from django.utils import timezone

from core.choices import RecoveryType, RecoveryActionType, ProgressStatus
from exams.models import AvailableTime
from planner.models import RecoveryPlan, RecoveryPlanItem, DailyPlanItem
from planner.services.scheduler import (
    TaskInput, AvailableTimeInput, allocate_tasks_to_days,
)
from planner.services.time_estimator import estimate_task_minutes, round_up_to_five

# 핵심 집중형에서 제외 후보를 고르는 3단계 우선순위.
# importance=high 또는 depth=core인 작업은 어떤 tier에도 해당하지 않아
# 절대 제외되지 않는다.
EXCLUSION_TIERS = [
    ("low", "optional"),
    ("low", "basic"),
    ("medium", "optional"),
]


def _remaining_minutes(item) -> int:
    """
    복구 시점의 최신 speed_factor로 예상시간을 다시 계산한다.
    최초 스케줄러가 estimated_max_minutes 기준으로 배치하므로,
    복구 계획도 같은 보수적 기준(estimated_max)을 유지한다.
    (평균값을 쓰면 복구 일정이 낙관적으로 잡혀 다시 무너질 위험이 커짐)
    """
    log = item.progress_log
    task = item.study_task
    exam = task.exam

    _estimated_min, estimated_max = estimate_task_minutes(
        task.task_type, task.difficulty, exam.speed_factor
    )

    if log.progress_status == ProgressStatus.NOT_DONE:
        return estimated_max

    # PARTIAL: 완료율만큼 뺀 나머지 비율만 적용
    remaining_ratio = (100 - log.completion_percent) / 100
    return round_up_to_five(estimated_max * remaining_ratio)


def _future_available_capacity(exam_period, from_date) -> list[AvailableTimeInput]:
    """
    복구 작업을 배치할 수 있는 날짜별 순수 잔여 가용시간.
    시작일은 항상 '내일'로 고정한다 (from_date가 과거여도 오늘 이전으로는
    절대 배치하지 않기 위함 — 늦게 마감해도 복구는 항상 내일부터 시작).
    """
    from datetime import timedelta
    start_date = max(
        from_date + timedelta(days=1),
        timezone.localdate() + timedelta(days=1),
    )

    available_times = AvailableTime.objects.filter(
        exam_period=exam_period, date__gte=start_date
    )
    occupied = dict(
        DailyPlanItem.objects.filter(
            daily_plan__exam_period=exam_period,
            daily_plan__date__gte=start_date,
        )
        .values('daily_plan__date')
        .annotate(total=Sum('planned_minutes'))
        .values_list('daily_plan__date', 'total')
    )
    return [
        AvailableTimeInput(
            date=at.date,
            available_minutes=max(
                at.available_minutes - occupied.get(at.date, 0), 0
            ),
        )
        for at in available_times
    ]


def _build_task_inputs(items_with_remaining) -> list[TaskInput]:
    return [
        TaskInput(
            id=item.id,
            exam_date=item.study_task.exam.exam_date,
            importance=item.study_task.importance,
            order=item.study_task.order,
            estimated_max_minutes=remaining,
            depth=item.study_task.depth,
        )
        for item, remaining in items_with_remaining
    ]


def _try_allocate(exam_period, from_date, items_with_remaining) -> dict:
    task_inputs = _build_task_inputs(items_with_remaining)
    available_time_inputs = _future_available_capacity(exam_period, from_date)
    return allocate_tasks_to_days(task_inputs, available_time_inputs)


def _exclusion_sort_key(item):
    """
    같은 tier 안에서 제외 후보를 고르는 순서.
    - 시험일이 먼 과목 우선 제외 (가까운 시험 보호)
      date는 직접 음수화할 수 없으므로 toordinal()로 정수 변환 후 음수화한다.
    - remaining_minutes 내림차순 (큰 것부터 빼야 제외 개수가 최소화됨)
    - order 내림차순, id 내림차순: 최종 타이브레이커
    """
    remaining = item._recovery_remaining_minutes
    return (
        -item.study_task.exam.exam_date.toordinal(),
        -remaining,
        -item.study_task.order,
        -item.id,
    )


def _try_core_focus_exclusion(exam_period, from_date, items_with_remaining):
    """
    3단계 tier를 순서대로 적용해 작업을 하나씩 제외하며 재배치를 시도한다.
    핵심 집중형은 분량 유지형과 구분되어야 하므로, 제외 없이 배치가 이미
    성공하는 상황이어도 최소 한 개는 제외한 뒤 결과를 만든다.

    importance=high 또는 depth=core인 작업은 후보에서 아예 제외한다.

    반환: 성공 시 {"remaining": [...], "excluded": [...], "allocation_result": {...}},
          실패(제외 가능한 후보 자체가 없거나 다 빼도 배치 불가) 시 None
    """
    remaining_items = list(items_with_remaining)
    excluded_items = []

    for item, remaining in remaining_items:
        item._recovery_remaining_minutes = remaining

    for tier_importance, tier_depth in EXCLUSION_TIERS:
        while True:
            candidates = [
                (item, remaining) for item, remaining in remaining_items
                if item.study_task.importance == tier_importance
                and item.study_task.depth == tier_depth
            ]
            if not candidates:
                break

            candidates.sort(key=lambda pair: _exclusion_sort_key(pair[0]))
            to_exclude = candidates[0]
            remaining_items.remove(to_exclude)
            excluded_items.append(to_exclude[0])

            if not remaining_items:
                return None

            result = _try_allocate(exam_period, from_date, remaining_items)
            if not result['unallocated_tasks']:
                return {
                    'remaining': remaining_items,
                    'excluded': excluded_items,
                    'allocation_result': result,
                }

    return None


def _create_recovery_plan(
    *,
    source_daily_plan,
    recovery_type,
    recovery_group_id,
    items_with_remaining,
    allocation_result,
    excluded_items=None,
):
    """
    이미 계산된 allocation_result를 그대로 저장만 한다 (재배치하지 않음).
    """
    if allocation_result.get('unallocated_tasks'):
        raise ValueError(
            "미배치 작업이 있는 결과로 RecoveryPlan을 생성할 수 없습니다."
        )

    recovery_plan = RecoveryPlan.objects.create(
        exam_period=source_daily_plan.exam_period,
        source_daily_plan=source_daily_plan,
        recovery_group_id=recovery_group_id,
        recovery_type=recovery_type,
    )

    items_by_id = {item.id: (item, remaining) for item, remaining in items_with_remaining}

    recovery_items = []
    for alloc in allocation_result['allocations']:
        item, remaining = items_by_id[alloc['task_id']]
        recovery_items.append(RecoveryPlanItem(
            recovery_plan=recovery_plan,
            study_task=item.study_task,
            original_date=item.daily_plan.date,
            changed_date=alloc['date'],
            action_type=RecoveryActionType.RESCHEDULE,
            remaining_minutes=remaining,
            reason="재배치: 가용시간 내 재계산",
        ))

    for item in (excluded_items or []):
        remaining = getattr(item, '_recovery_remaining_minutes', 0)
        recovery_items.append(RecoveryPlanItem(
            recovery_plan=recovery_plan,
            study_task=item.study_task,
            original_date=item.daily_plan.date,
            changed_date=None,
            action_type=RecoveryActionType.EXCLUDE,
            remaining_minutes=remaining,
            reason="핵심 집중형: 우선순위 낮은 작업 단계적 제외",
        ))

    RecoveryPlanItem.objects.bulk_create(recovery_items)
    return recovery_plan


def generate_recovery_options(daily_plan, unfinished_items) -> dict:
    exam_period = daily_plan.exam_period
    from_date = daily_plan.date
    recovery_group_id = uuid.uuid4()

    items_with_remaining = [
        (item, _remaining_minutes(item)) for item in unfinished_items
    ]

    maintain_volume_allocation = _try_allocate(
        exam_period, from_date, items_with_remaining
    )
    maintain_volume = None
    maintain_volume_failure_reason = None
    if maintain_volume_allocation['unallocated_tasks']:
        maintain_volume_failure_reason = "남은 가용시간이 부족합니다."
    else:
        maintain_volume = _create_recovery_plan(
            source_daily_plan=daily_plan,
            recovery_type=RecoveryType.MAINTAIN_VOLUME,
            recovery_group_id=recovery_group_id,
            items_with_remaining=items_with_remaining,
            allocation_result=maintain_volume_allocation,
        )

    core_focus_result = _try_core_focus_exclusion(
        exam_period, from_date, items_with_remaining
    )

    core_focus = None
    core_focus_failure_reason = None
    if core_focus_result is not None:
        core_focus = _create_recovery_plan(
            source_daily_plan=daily_plan,
            recovery_type=RecoveryType.CORE_FOCUS,
            recovery_group_id=recovery_group_id,
            items_with_remaining=core_focus_result['remaining'],
            allocation_result=core_focus_result['allocation_result'],
            excluded_items=core_focus_result['excluded'],
        )
    else:
        core_focus_failure_reason = "제외 가능한 작업을 줄여도 배치할 수 없습니다."

    return {
        'maintain_volume': maintain_volume,
        'maintain_volume_failure_reason': maintain_volume_failure_reason,
        'core_focus': core_focus,
        'core_focus_failure_reason': core_focus_failure_reason,
    }