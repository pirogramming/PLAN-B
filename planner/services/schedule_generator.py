"""
allocate_tasks_to_days()의 배치 결과를 실제 DB(DailyPlan, DailyPlanItem)에
저장하는 모듈. scheduler.py의 순수 계산 로직과 분리했다.

주의: select_for_update()는 SQLite에서는 row-level locking을 지원하지 않아
      no-op으로 처리된다 (에러 없이 조용히 무시됨). 로컬 SQLite 테스트로는
      동시 요청 방지 효과를 검증할 수 없고, PostgreSQL 등 실제 운영 DB에서만
      의미 있게 작동한다.
"""
from django.db import transaction

from planner.models import DailyPlan, DailyPlanItem
from planner.services.scheduler import (
    TaskInput,
    AvailableTimeInput,
    allocate_tasks_to_days,
)


class ScheduleAlreadyExistsError(Exception):
    pass


class UnallocatedTasksError(Exception):
    def __init__(self, unallocated_task_ids):
        self.unallocated_task_ids = unallocated_task_ids
        super().__init__(f"배치되지 못한 작업: {unallocated_task_ids}")


class DuplicateTaskAllocationError(Exception):
    pass


class MismatchedExamPeriodError(Exception):
    pass


class ScheduleHasProgressError(Exception):
    """
    이미 진행 기록(ProgressLog)이 존재하는 계획을 통째로 재생성하려 할 때
    발생시킨다.

    DailyPlanItem 삭제는 ProgressLog와 CASCADE로 연결돼 있어서, 이 검증
    없이 replace_existing=True로 재생성하면 사용자가 이미 입력한 공부
    기록이 조용히 사라진다. 학습 시작 이후의 일정 변경은 여기(전체 재생성)가
    아니라 복구안(recovery) 기능으로 처리해야 한다.
    """
    pass


def _validate_belongs_to_exam_period(study_tasks, available_times, exam_period):
    """study_tasks와 available_times가 모두 이 exam_period 소속인지 확인한다."""
    for task in study_tasks:
        if task.exam.exam_period_id != exam_period.id:
            raise MismatchedExamPeriodError(
                f"StudyTask(id={task.id})가 다른 exam_period에 속해 있습니다."
            )
    for at in available_times:
        at_exam_period_id = getattr(at, "exam_period_id", None)
        if at_exam_period_id is not None and at_exam_period_id != exam_period.id:
            raise MismatchedExamPeriodError(
                f"AvailableTime(date={at.date})이 다른 exam_period에 속해 있습니다."
            )


def _to_task_inputs(study_tasks):
    return [
        TaskInput(
            id=task.id,
            exam_date=task.exam.exam_date,
            importance=task.importance,
            depth=task.depth,
            order=task.order,
            estimated_max_minutes=task.estimated_max_minutes,
        )
        for task in study_tasks
    ]


def _to_available_time_inputs(available_times):
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
        1. exam_period 소속 검증
        2. 배치 계산 (allocate_tasks_to_days) - DB 변경 없음
        3. 미배치 작업 검증 - 하나라도 있으면 즉시 중단, DB 미변경
        4. 기존 계획 처리 (동시성 잠금 포함, SQLite에서는 무의미함에 유의)
           - 진행 기록(ProgressLog)이 하나라도 있으면 재생성 자체를 거부한다.
             DailyPlanItem 삭제가 ProgressLog까지 CASCADE로 지워버리기 때문에,
             학습 시작 이후에는 여기가 아니라 복구안(recovery) 기능으로
             일정을 바꿔야 한다.
        5. 새 계획 저장

    Raises:
        MismatchedExamPeriodError: study_tasks/available_times가 다른 exam_period 소속일 때
        UnallocatedTasksError: 배치 못한 작업이 하나라도 있을 때
        ScheduleAlreadyExistsError: 기존 계획이 있고 replace_existing=False일 때
        ScheduleHasProgressError: replace_existing=True로 전체 재생성을
            요청했지만, 기존 계획에 진행 기록이 존재하는 경우
        DuplicateTaskAllocationError: 배치 결과에 같은 작업이 중복 등장할 때
    """
    _validate_belongs_to_exam_period(study_tasks, available_times, exam_period)

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

    if unallocated_task_ids:
        raise UnallocatedTasksError(unallocated_task_ids)

    allocated_task_ids = [a["task_id"] for a in allocations]
    if len(allocated_task_ids) != len(set(allocated_task_ids)):
        raise DuplicateTaskAllocationError()

    locked_exam_period = (
        type(exam_period).objects.select_for_update().get(pk=exam_period.pk)
    )

    existing_items = DailyPlanItem.objects.filter(
        daily_plan__exam_period=locked_exam_period,
    )

    if existing_items.exists():
        if not replace_existing:
            raise ScheduleAlreadyExistsError()

        # 진행 기록이 하나라도 있으면 replace_existing=True여도 재생성을
        # 막는다. 여기서 막지 않으면 DailyPlanItem 삭제가 ProgressLog까지
        # CASCADE로 조용히 지워버린다 (사용자 공부 기록 유실).
        if existing_items.filter(progress_log__isnull=False).exists():
            raise ScheduleHasProgressError(
                "진행 기록이 존재하는 계획은 전체 재생성할 수 없습니다. "
                "복구안(recovery) 기능을 사용해주세요."
            )

        existing_items.delete()
        DailyPlan.objects.filter(exam_period=locked_exam_period).delete()

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