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