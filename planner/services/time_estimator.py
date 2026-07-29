"""
학습 작업의 예상 소요시간을 계산하는 순수 함수 모듈.
DB 저장은 하지 않고, StudyTask 생성/수정 시 호출되어
estimated_min_minutes, estimated_max_minutes 값을 산출하는 데 쓰인다.
"""
import math

# 작업 유형별 기준시간(분): (최소, 최대)
TASK_TYPE_BASE_MINUTES = {
    "concept": (20, 40),
    "practice": (30, 60),
    "review": (15, 30),
    "summary": (20, 35),
    "custom": (20, 40),
}

# 난이도별 배율
DIFFICULTY_MULTIPLIERS = {
    "easy": 0.8,
    "normal": 1.0,
    "hard": 1.3,
}

DEFAULT_BASE_MINUTES = TASK_TYPE_BASE_MINUTES["custom"]
DEFAULT_DIFFICULTY_MULTIPLIER = DIFFICULTY_MULTIPLIERS["normal"]


def round_up_to_five(minutes: float) -> int:
    """분 단위 값을 5분 단위로 올림 처리한다."""
    return math.ceil(minutes / 5) * 5


def estimate_task_minutes(
    task_type: str,
    difficulty: str,
    speed_factor: float,
) -> tuple[int, int]:
    """
    작업 유형, 난이도, 과목별 속도계수를 기반으로
    예상 최소/최대 소요시간(분)을 계산한다.

    depth는 시간 계산에 사용하지 않는다.
    (depth는 recovery.py의 핵심 집중형 복구안에서만 사용)

    Returns:
        (estimated_min_minutes, estimated_max_minutes)
    """
    base_min, base_max = TASK_TYPE_BASE_MINUTES.get(
        task_type, DEFAULT_BASE_MINUTES
    )
    difficulty_multiplier = DIFFICULTY_MULTIPLIERS.get(
        difficulty, DEFAULT_DIFFICULTY_MULTIPLIER
    )

    raw_min = base_min * difficulty_multiplier * speed_factor
    raw_max = base_max * difficulty_multiplier * speed_factor

    estimated_min = round_up_to_five(raw_min)
    estimated_max = round_up_to_five(raw_max)

    return estimated_min, estimated_max