"""
확정된 StudyTask를 날짜별 AvailableTime에 자동 배치하는 모듈.

allocate_tasks_to_days()는 DB를 건드리지 않는 순수 함수이며,
ORM 객체 대신 가벼운 dataclass(TaskInput, AvailableTimeInput)를 입력으로 받는다.
실제 ORM → dataclass 변환과 DB 저장은 generate_schedule()(또는 이를 호출하는
selectors/views 계층)에서 담당한다.
"""
from dataclasses import dataclass
from datetime import date as date_type

IMPORTANCE_PRIORITY = {
    "high": 0,
    "medium": 1,
    "low": 2,
}
DEFAULT_IMPORTANCE_PRIORITY = IMPORTANCE_PRIORITY["medium"]


@dataclass(frozen=True)
class TaskInput:
    id: int
    exam_date: date_type
    importance: str
    order: int
    estimated_max_minutes: int


@dataclass(frozen=True)
class AvailableTimeInput:
    date: date_type
    available_minutes: int


def _sort_key(task: TaskInput):
    importance_rank = IMPORTANCE_PRIORITY.get(
        task.importance, DEFAULT_IMPORTANCE_PRIORITY
    )
    return (task.exam_date, importance_rank, task.order, task.id)


def allocate_tasks_to_days(
    tasks: list[TaskInput],
    available_times: list[AvailableTimeInput],
) -> dict:
    """
    작업들을 날짜별 가용시간에 배치한다.

    배치 규칙:
        - 정렬: 시험일 오름차순 -> 중요도(high>medium>low) -> order -> id
        - 각 작업은 해당 과목의 exam_date 이전 날짜에만 배치 가능
        - 남은 시간이 estimated_max_minutes 이상인, 배치 가능한 날짜 중
          가장 빠른 날짜에 배치
        - 작업을 쪼개지 않음: 들어갈 자리가 없으면 unallocated_tasks로 반환

    Returns:
        {
            "allocations": [
                {"task_id": int, "date": date, "allocated_minutes": int},
                ...
            ],
            "unallocated_tasks": [task_id, ...],
        }
    """
    sorted_tasks = sorted(tasks, key=_sort_key)

    # 날짜별 잔여시간은 원본을 건드리지 않도록 내부 복사본으로 관리
    remaining_minutes = {
        at.date: at.available_minutes for at in available_times
    }
    sorted_dates = sorted(remaining_minutes.keys())

    allocations = []
    unallocated_tasks = []

    for task in sorted_tasks:
        placed = False
        for day in sorted_dates:
            if day >= task.exam_date:
                continue
            if remaining_minutes[day] >= task.estimated_max_minutes:
                remaining_minutes[day] -= task.estimated_max_minutes
                allocations.append(
                    {
                        "task_id": task.id,
                        "date": day,
                        "allocated_minutes": task.estimated_max_minutes,
                    }
                )
                placed = True
                break
        if not placed:
            unallocated_tasks.append(task.id)

    return {
        "allocations": allocations,
        "unallocated_tasks": unallocated_tasks,
    }

from django.db import transaction

from planner.models import DailyPlan, DailyPlanItem


class ScheduleAlreadyExistsError(Exception):
    pass


class UnallocatedTasksError(Exception):
    def __init__(self, unallocated_task_ids):
        self.unallocated_task_ids = unallocated_task_ids
        super().__init__(f"배치되지 못한 작업: {unallocated_task_ids}")


class DuplicateTaskAllocationError(Exception):
    pass


def _to_task_inputs(study_tasks):
    """StudyTask 쿼리셋/리스트를 TaskInput dataclass 리스트로 변환한다."""
    return [
        TaskInput(
            id=task.id,
            exam_date=task.exam.exam_date,
            importance=task.importance,
            order=task.order,
            estimated_max_minutes=task.estimated_max_minutes,
        )
        for task in study_tasks
    ]


def _to_available_time_inputs(available_times):
    """AvailableTime 쿼리셋/리스트를 AvailableTimeInput dataclass 리스트로 변환한다."""
    return [
        AvailableTimeInput(date=at.date, available_minutes=at.available_minutes)
        for at in available_times
    ]


@transaction.atomic
def generate_schedule(
    *,
    exam_period,
    study_tasks,
    available_times,
    replace_existing=False,
):
    """
    확정된 StudyTask들을 날짜별 DailyPlan/DailyPlanItem으로 저장한다.

    처리 순서 (반드시 이 순서를 지킨다):
        1. 배치 계산 (allocate_tasks_to_days) - DB 변경 없음
        2. 미배치 작업 검증 - 하나라도 있으면 즉시 중단, DB 미변경
        3. 기존 계획 처리 (동시성 잠금 포함)
        4. 새 계획 저장

    Raises:
        UnallocatedTasksError: 배치 못한 작업이 하나라도 있을 때
        ScheduleAlreadyExistsError: 기존 계획이 있고 replace_existing=False일 때
        DuplicateTaskAllocationError: 배치 결과에 같은 작업이 중복 등장할 때
    """
    task_inputs = _to_task_inputs(study_tasks)
    task_lookup = {task.id: task for task in study_tasks}

    available_time_inputs = _to_available_time_inputs(available_times)
    available_minutes_by_date = {
        at.date: at.available_minutes for at in available_time_inputs
    }

    allocation_result = allocate_tasks_to_days(
        tasks=task_inputs,
        available_times=available_time_inputs,
    )
    allocations = allocation_result["allocations"]
    unallocated_task_ids = allocation_result["unallocated_tasks"]

    # 2. 미배치 검증을 기존 계획 삭제보다 먼저 수행 (부분 저장 방지)
    if unallocated_task_ids:
        raise UnallocatedTasksError(unallocated_task_ids)

    allocated_task_ids = [a["task_id"] for a in allocations]
    if len(allocated_task_ids) != len(set(allocated_task_ids)):
        raise DuplicateTaskAllocationError()

    # 3. 동시 요청 방지를 위해 ExamPeriod 행 잠금
    locked_exam_period = (
        type(exam_period).objects.select_for_update().get(pk=exam_period.pk)
    )

    existing_items = DailyPlanItem.objects.filter(
        daily_plan__exam_period=locked_exam_period,
    )

    if existing_items.exists():
        if not replace_existing:
            raise ScheduleAlreadyExistsError()
        existing_items.delete()
        DailyPlan.objects.filter(exam_period=locked_exam_period).delete()

    # 4. 새 계획 저장
    daily_plans_by_date = {}
    items_to_create = []
    order_counter_by_date = {}

    for allocation in allocations:
        day = allocation["date"]

        if day not in daily_plans_by_date:
            daily_plan, _ = DailyPlan.objects.get_or_create(
                exam_period=locked_exam_period,
                date=day,
                defaults={
                    "available_minutes": available_minutes_by_date.get(day, 0),
                    "planned_minutes": 0,
                },
            )
            daily_plans_by_date[day] = daily_plan
            order_counter_by_date[day] = 0

        order_counter_by_date[day] += 1
        items_to_create.append(
            DailyPlanItem(
                daily_plan=daily_plans_by_date[day],
                study_task=task_lookup[allocation["task_id"]],
                planned_minutes=allocation["allocated_minutes"],
                order=order_counter_by_date[day],
            )
        )

    DailyPlanItem.objects.bulk_create(items_to_create)

    # DailyPlan.planned_minutes를 실제 배치된 합계로 갱신
    for day, daily_plan in daily_plans_by_date.items():
        total = sum(
            item.planned_minutes
            for item in items_to_create
            if item.daily_plan_id == daily_plan.id
        )
        daily_plan.planned_minutes = total
        daily_plan.save(update_fields=["planned_minutes"])

    return {
        "daily_plans": list(daily_plans_by_date.values()),
        "created_item_count": len(items_to_create),
        "unallocated_tasks": [],
    }