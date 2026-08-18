import logging
import math

logger = logging.getLogger(__name__)

# 작업 유형별 기준시간(분): (최소, 최대)
TASK_TYPE_BASE_MINUTES = {
    "concept": (15, 30),
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
    if task_type not in TASK_TYPE_BASE_MINUTES:
        logger.warning(
            "알 수 없는 task_type=%r 이 들어와 기본값(custom)으로 대체합니다.",
            task_type,
        )
    if difficulty not in DIFFICULTY_MULTIPLIERS:
        logger.warning(
            "알 수 없는 difficulty=%r 이 들어와 기본값(normal)으로 대체합니다.",
            difficulty,
        )

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