"""
과목별 speed_factor를 재계산하는 모듈.

record_progress()가 로그를 저장/수정할 때마다 이 함수를 호출해
"기존 값에 새 비율을 누적 반영"하지 않고, 해당 과목의 유효한
ProgressLog 전체를 처음부터 다시 계산한다.
(누적 방식은 로그를 수정할 때 이전 기록이 이중으로 반영되는 문제가 있다.)
"""
from core.choices import ProgressStatus

MIN_SPEED_FACTOR = 0.5
MAX_SPEED_FACTOR = 2.0
BLEND_WEIGHT = 0.3  # 새 비율을 30% 반영


def _clamp(value, lower, upper):
    return max(lower, min(upper, value))


def _is_valid_log(log):
    """speed_factor 계산에 쓸 수 있는 유효한 기록인지 확인한다."""
    if log.progress_status == ProgressStatus.NOT_DONE:
        return False
    if not log.completion_percent or log.completion_percent <= 0:
        return False
    if not log.actual_minutes or log.actual_minutes <= 0:
        return False
    task = log.daily_plan_item.study_task
    if not task.estimated_min_minutes and not task.estimated_max_minutes:
        return False
    return True


def _ratio_for_log(log):
    task = log.daily_plan_item.study_task
    base_expected_minutes = (
        task.estimated_min_minutes + task.estimated_max_minutes
    ) / 2
    expected_for_completed_part = (
        base_expected_minutes * log.completion_percent / 100
    )
    if expected_for_completed_part <= 0:
        return None
    return log.actual_minutes / expected_for_completed_part


def calculate_speed_factor(logs) -> float:
    """
    로그 목록(기록 시각 오름차순)을 받아 speed_factor를 처음부터 재계산하는
    순수 함수. 유효 기록이 하나도 없으면 기본값 1.0을 반환한다.
    """
    factor = 1.0
    has_valid_log = False

    for log in logs:
        if not _is_valid_log(log):
            continue
        ratio = _ratio_for_log(log)
        if ratio is None:
            continue
        factor = factor * (1 - BLEND_WEIGHT) + ratio * BLEND_WEIGHT
        has_valid_log = True

    if not has_valid_log:
        return 1.0

    return round(_clamp(factor, MIN_SPEED_FACTOR, MAX_SPEED_FACTOR), 3)


def recalculate_speed_factor(exam):
    """
    exam에 속한 모든 ProgressLog를 기록 시각 순으로 조회해
    speed_factor를 재계산하고 exam에 저장한다.
    """
    from planner.models import ProgressLog

    logs = (
        ProgressLog.objects.filter(
            daily_plan_item__study_task__exam=exam,
        )
        .select_related("daily_plan_item__study_task")
        .order_by("recorded_at")
    )

    exam.speed_factor = calculate_speed_factor(list(logs))
    exam.save(update_fields=["speed_factor"])
    return exam.speed_factor