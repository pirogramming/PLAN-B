"""
finalize_daily_plan()에서 넘겨받은 미완료 작업(PARTIAL/NOT_DONE)을 기준으로
분량유지형/핵심집중형 복구안 2종을 계산만 하고 저장한다.
DailyPlan/DailyPlanItem은 건드리지 않는다 (사용자 승인 전까지 미적용).
"""
import uuid
from collections import defaultdict

from django.db import models, transaction
from django.db.models import Sum
from django.utils import timezone

from core.choices import RecoveryType, RecoveryActionType, ProgressStatus, RecoveryPlanStatus
from exams.models import AvailableTime
from planner.models import RecoveryPlan, RecoveryPlanItem, DailyPlanItem, DailyPlan
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
    recovery_plan.full_clean()
    
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

def needs_recovery_retry(daily_plan) -> bool:
    """
    마감됐고, 미완료(PARTIAL/NOT_DONE) 항목이 있는데, RecoveryPlan이 하나도
    생성되지 않은(=마감 시점에 복구안 생성이 두 안 다 실패했던) 상태인지 판별한다.
    새 필드 없이, generate_recovery_options()가 실패 시 RecoveryPlan을 아예
    만들지 않는다는 사실을 이용한다.
    """
    if daily_plan.finalized_at is None:
        return False

    has_unfinished = DailyPlanItem.objects.filter(
        daily_plan=daily_plan,
        progress_log__progress_status__in=(
            ProgressStatus.PARTIAL, ProgressStatus.NOT_DONE
        ),
    ).exists()
    if not has_unfinished:
        return False

    return not RecoveryPlan.objects.filter(source_daily_plan=daily_plan).exists()


def retry_recovery_generation(daily_plan) -> dict:
    """
    마감된 daily_plan을 source로, 현재 최신 AvailableTime 기준으로 복구안
    생성을 다시 시도한다. 마감 상태(finalized_at)와 NOT_DONE/PARTIAL 기록은
    건드리지 않는다 - 그날의 실제 기록은 그대로 두고 복구안만 새로 계산한다.

    동시 요청 방어: DailyPlan row를 잠근 뒤 필요 여부를 다시 확인한다.
    두 요청이 거의 동시에 들어와도, 먼저 잠금을 획득한 쪽이 RecoveryPlan을
    만들고 커밋하면, 뒤이어 잠금을 획득한 쪽은 재확인 시점에 이미 RecoveryPlan이
    생겨있는 걸 보고 RecoveryRetryNotNeededError로 빠진다 (중복 생성 방지).

    Raises:
        RecoveryRetryNotNeededError: 재생성이 필요한 상태가 아닌 경우
            (아직 미마감, 미완료 항목 없음, 또는 이미 RecoveryPlan이 있음)
    """
    with transaction.atomic():
        locked_plan = (
            DailyPlan.objects.select_for_update().get(pk=daily_plan.pk)
        )

        if not needs_recovery_retry(locked_plan):
            raise RecoveryRetryNotNeededError(
                f"{locked_plan}는 복구안 재생성이 필요한 상태가 아닙니다."
            )

        unfinished_items = list(
            DailyPlanItem.objects.filter(
                daily_plan=locked_plan,
                progress_log__progress_status__in=(
                    ProgressStatus.PARTIAL, ProgressStatus.NOT_DONE
                ),
            ).select_related('progress_log', 'study_task__exam')
        )

        return generate_recovery_options(locked_plan, unfinished_items)


class RecoveryRetryNotNeededError(Exception):
    """재생성이 필요한 상태가 아닐 때 발생한다."""

class RecoveryPlanAlreadyProcessedError(Exception):
    pass


class RecoveryPlanStaleError(Exception):
    pass

class RecoveryPlanInvalidDataError(Exception):
    """
    복구안 자체의 계산 결과가 유효하지 않은 경우 (예: remaining_minutes <= 0).
    RecoveryPlanStaleError와 달리 '시점 경과로 인한 불일치'가 아니라
    생성 당시부터 잘못된 값이 저장됐을 가능성을 가리킨다.
    """
    pass


def _get_or_create_daily_plan(exam_period, date):
    """
    changed_date에 해당하는 DailyPlan을 가져오거나 새로 만든다.
    이미 존재하는 DailyPlan이어도 available_minutes는 최신 AvailableTime
    기준으로 동기화한다 (stale 검증이 최신 값을 기준으로 하므로 일관성 유지).
    """
    try:
        available_time = AvailableTime.objects.get(
            exam_period=exam_period, date=date
        )
    except AvailableTime.DoesNotExist as exc:
        raise RecoveryPlanStaleError(
            f"{date}의 가용시간이 더 이상 존재하지 않습니다."
        ) from exc

    daily_plan, created = DailyPlan.objects.get_or_create(
        exam_period=exam_period,
        date=date,
        defaults={
            'available_minutes': available_time.available_minutes,
            'planned_minutes': 0,
        },
    )

    if not created and daily_plan.available_minutes != available_time.available_minutes:
        daily_plan.available_minutes = available_time.available_minutes
        daily_plan.save(update_fields=['available_minutes'])

    return daily_plan


def _validate_not_stale(exam_period, items_by_date):
    """
    복구안 계산 이후 가용시간/기존 일정/시험일/날짜 경과 여부가 바뀌었을
    수 있으므로 적용 직전에 changed_date별로 다시 검증한다.
    문제가 있으면 RecoveryPlanStaleError를 발생시켜 전체 트랜잭션을 롤백시킨다.
    """
    today = timezone.localdate()

    for changed_date, date_items in items_by_date.items():
        if changed_date <= today:
            raise RecoveryPlanStaleError(
                f"{changed_date}는 이미 지난 배치 날짜입니다. 복구안을 다시 생성해주세요."
            )

        existing_daily_plan = (
            DailyPlan.objects
            .select_for_update()
            .filter(exam_period=exam_period, date=changed_date)
            .first()
        )
        if existing_daily_plan is not None and existing_daily_plan.finalized_at is not None:
            raise RecoveryPlanStaleError(
                f"{changed_date} 계획은 이미 마감되어 수정할 수 없습니다."
            )

        try:
            available_time = AvailableTime.objects.get(
                exam_period=exam_period, date=changed_date
            )
        except AvailableTime.DoesNotExist:
            raise RecoveryPlanStaleError(
                f"{changed_date}의 가용시간이 더 이상 존재하지 않습니다."
            )

        occupied = DailyPlanItem.objects.filter(
            daily_plan__exam_period=exam_period,
            daily_plan__date=changed_date,
        ).aggregate(total=Sum('planned_minutes'))['total'] or 0

        needed = sum(item.remaining_minutes for item in date_items)
        remaining_capacity = available_time.available_minutes - occupied
        if needed > remaining_capacity:
            raise RecoveryPlanStaleError(
                f"{changed_date}의 가용시간이 부족합니다 "
                f"(필요: {needed}분, 남은 용량: {remaining_capacity}분)."
            )

        for item in date_items:
            if item.study_task.exam.exam_date <= changed_date:
                raise RecoveryPlanStaleError(
                    f"{item.study_task}의 시험일이 지나 이 날짜에 배치할 수 없습니다."
                )


def apply_recovery_plan(recovery_plan) -> dict:
    """
    사용자가 선택한 복구안을 실제 일정에 반영한다.

    - 같은 recovery_group_id의 모든 RecoveryPlan을 잠그고, 그룹 내에
      이미 APPLIED된 안이 있으면 거부한다 (분량유지형/핵심집중형 동시 적용 방지)
    - 선택한 복구안이 PENDING이 아니면 거부
    - 적용 직전 changed_date별 가용시간/기존 배치/시험일을 재검증하고,
      문제가 있으면 RecoveryPlanStaleError로 전체 롤백
    - RESCHEDULE 항목만 changed_date의 DailyPlan에 새 DailyPlanItem으로 생성
    - EXCLUDE 항목은 새 일정에 생성하지 않음
    - 원본(과거) DailyPlanItem/ProgressLog는 건드리지 않고 그대로 보존
    - 선택한 복구안은 APPLIED, 같은 그룹의 나머지 PENDING은 DISCARDED
    
    ...(기존 docstring)...
    잠금 순서 컨벤션: RecoveryPlan → DailyPlan 순으로 잠근다.
    다른 서비스 함수를 추가할 때도 이 순서를 지켜야 데드락을 피할 수 있다.
    """

    with transaction.atomic():
        group_id = recovery_plan.recovery_group_id
        group_plans = list(
            RecoveryPlan.objects
            .select_for_update()
            .filter(recovery_group_id=group_id)
            .order_by('pk')
        )
        target = next(p for p in group_plans if p.pk == recovery_plan.pk)

        if any(p.status == RecoveryPlanStatus.APPLIED for p in group_plans):
            raise RecoveryPlanAlreadyProcessedError(
                f"{group_id} 그룹에는 이미 적용된 복구안이 있습니다."
            )
        if target.status != RecoveryPlanStatus.PENDING:
            raise RecoveryPlanAlreadyProcessedError(
                f"{target}는 이미 처리된 복구안입니다."
            )

        items = list(target.items.select_related('study_task__exam'))
        reschedule_items = [
            item for item in items
            if item.action_type == RecoveryActionType.RESCHEDULE
        ]

        items_by_date = defaultdict(list)
        for item in reschedule_items:
            if item.changed_date is None:
                raise RecoveryPlanStaleError(
                    f"{item.study_task}의 재배치 날짜가 없습니다."
                )
            if item.remaining_minutes <= 0:
                raise RecoveryPlanInvalidDataError(
                    f"{item.study_task}의 remaining_minutes가 {item.remaining_minutes}로 "
                    f"유효하지 않습니다. 복구안 생성 로직을 확인해야 합니다."
                )
            items_by_date[item.changed_date].append(item)

        _validate_not_stale(target.exam_period, items_by_date)

        created_items = []
        for changed_date, date_items in items_by_date.items():
            daily_plan = _get_or_create_daily_plan(target.exam_period, changed_date)

            next_order = (
                daily_plan.items.aggregate(models.Max('order'))['order__max'] or 0
            )
            for item in date_items:
                next_order += 1
                new_item = DailyPlanItem.objects.create(
                    daily_plan=daily_plan,
                    study_task=item.study_task,
                    planned_minutes=item.remaining_minutes,
                    order=next_order,
                )
                created_items.append(new_item)

            daily_plan.planned_minutes = (
                daily_plan.items.aggregate(total=Sum('planned_minutes'))['total'] or 0
            )
            daily_plan.save(update_fields=['planned_minutes'])

        target.status = RecoveryPlanStatus.APPLIED
        target.applied_at = timezone.now()
        target.save(update_fields=['status', 'applied_at'])

        RecoveryPlan.objects.filter(
            recovery_group_id=group_id, status=RecoveryPlanStatus.PENDING,
        ).exclude(pk=target.pk).update(status=RecoveryPlanStatus.DISCARDED)

        return {
            'recovery_plan': target,
            'created_daily_plan_items': created_items,
        }

def get_future_available_capacity(exam_period, from_date) -> list[AvailableTimeInput]:
    """복구에 사용할 수 있는 날짜별 순수 잔여 가용시간."""
    return _future_available_capacity(exam_period, from_date)


def get_future_available_minutes(exam_period, from_date) -> int:
    """복구에 사용할 수 있는 미래 잔여 가용시간의 총합."""
    return sum(
        item.available_minutes
        for item in get_future_available_capacity(exam_period, from_date)
    )