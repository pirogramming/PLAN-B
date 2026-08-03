"""
확정된 StudyTask를 날짜별 AvailableTime에 배치하는 순수 계산 모듈.

allocate_tasks_to_days()는 DB를 건드리지 않는 순수 함수이며,
ORM 객체 대신 가벼운 dataclass(TaskInput, AvailableTimeInput)를 입력으로 받는다.
실제 ORM 연동과 DB 저장은 schedule_generator.py에서 담당한다.
"""
from dataclasses import dataclass
from datetime import date as date_type

IMPORTANCE_PRIORITY = {
    "high": 0,
    "medium": 1,
    "low": 2,
}
DEFAULT_IMPORTANCE_PRIORITY = IMPORTANCE_PRIORITY["medium"]

DEPTH_PRIORITY = {
    "core": 0,
    "basic": 1,
    "optional": 2,
}
DEFAULT_DEPTH_PRIORITY = DEPTH_PRIORITY["basic"]


@dataclass(frozen=True)
class TaskInput:
    id: int
    exam_date: date_type
    importance: str
    order: int
    estimated_max_minutes: int
    # StudyTask.depth 기본값(basic)과 동일하게 맞춰서, depth를 아직 모르는
    # 기존 호출부·테스트가 깨지지 않게 한다. 실제 스케줄링에서는
    # schedule_generator._to_task_inputs()가 항상 실제 값을 채워 넣는다.
    depth: str = "basic"


@dataclass(frozen=True)
class AvailableTimeInput:
    date: date_type
    available_minutes: int


def _sort_key(task: TaskInput):
    """
    작업 처리 순서를 정하는 키.

    시험일 -> 중요도 -> 깊이(core>basic>optional)까지는 "어떤 작업이 부족한
    자리를 우선 차지할지"를 결정하는 정책 순서라서 그대로 둔다. 깊이가 큰
    작업 우선(공간 파편화 방지)보다 앞에 오는 이유는, 배치 성공률보다
    "핵심 작업이 먼저 자리를 차지해야 한다"는 정책이 우선이기 때문이다.

    그 다음에 estimated_max_minutes 내림차순을 추가한 이유(Best-Fit Decreasing):
    같은 시험일·중요도·깊이 안에서 작은 작업부터 넣으면(First-Fit) 큰 작업이
    들어갈 자리가 없어 "총 가용시간은 충분한데 미배치가 발생하는" 공간
    파편화가 생긴다. 큰 작업을 먼저 배치하면 이 문제가 크게 줄어든다.

    order/id는 같은 시험일·중요도·깊이·소요시간 안에서의 최종 타이브레이커로만
    쓴다.
    """
    importance_rank = IMPORTANCE_PRIORITY.get(
        task.importance, DEFAULT_IMPORTANCE_PRIORITY
    )
    depth_rank = DEPTH_PRIORITY.get(task.depth, DEFAULT_DEPTH_PRIORITY)
    return (
        task.exam_date,
        importance_rank,
        depth_rank,
        -task.estimated_max_minutes,
        task.order,
        task.id,
    )


def _find_best_fit_day(
    task: TaskInput,
    sorted_dates: list[date_type],
    remaining_minutes: dict,
):
    """
    작업이 배치 가능한 날짜(시험일 이전 + 남은 시간이 충분한 날짜) 중,
    남은 시간이 가장 적은(=가장 빡빡하게 맞는) 날짜를 고른다 (Best-Fit).

    First-Fit(그냥 가장 빠른 날짜)과 달리, 넉넉한 날짜를 함부로 먼저
    갉아먹지 않아서 뒤에 오는 큰 작업이 들어갈 자리를 남겨준다.
    남은 시간이 같은 후보끼리는 더 빠른 날짜를 선택한다.
    단, 전체적으로는 "가장 빠른 날짜"보다 "공간 활용도"를 우선하므로
    기존의 "가장 빠른 날짜 우선" 정책 자체는 이 변경으로 바뀐다.
    """
    best_day = None
    best_remaining = None
    for day in sorted_dates:
        if day >= task.exam_date:
            continue
        remaining = remaining_minutes[day]
        if remaining < task.estimated_max_minutes:
            continue
        if best_remaining is None or remaining < best_remaining:
            best_day = day
            best_remaining = remaining
    return best_day


def allocate_tasks_to_days(
    tasks: list[TaskInput],
    available_times: list[AvailableTimeInput],
) -> dict:
    """
    작업들을 날짜별 가용시간에 배치한다.

    배치 규칙:
        - 정렬: 시험일 오름차순 -> 중요도(high>medium>low)
                -> 깊이(core>basic>optional) -> estimated_max_minutes 내림차순
                -> order -> id
        - 각 작업은 해당 과목의 exam_date 이전 날짜에만 배치 가능
        - 배치 가능한 날짜 중 남은 시간이 가장 적은(Best-Fit) 날짜에 배치
          (동률이면 가장 빠른 날짜)
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

    remaining_minutes = {
        at.date: at.available_minutes for at in available_times
    }
    sorted_dates = sorted(remaining_minutes.keys())

    allocations = []
    unallocated_tasks = []

    for task in sorted_tasks:
        best_day = _find_best_fit_day(task, sorted_dates, remaining_minutes)
        if best_day is not None:
            remaining_minutes[best_day] -= task.estimated_max_minutes
            allocations.append(
                {
                    "task_id": task.id,
                    "date": best_day,
                    "allocated_minutes": task.estimated_max_minutes,
                }
            )
        else:
            unallocated_tasks.append(task.id)

    return {
        "allocations": allocations,
        "unallocated_tasks": unallocated_tasks,
    }