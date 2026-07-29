"""
시험 범위 전체의 실현 가능성을 판정하는 순수 함수 모듈.
DB 저장은 하지 않고, StudyTask들의 예상시간 합계(A, B)와
AvailableTime들의 가용시간 합계(C)를 받아 판정 결과를 반환한다.
"""

POSSIBLE = "possible"
RISKY = "risky"
IMPOSSIBLE = "impossible"


def calculate_feasibility(
    required_min_minutes: int,
    required_max_minutes: int,
    available_minutes: int,
) -> dict:
    """
    판정 기준:
        C >= B       -> possible (가능)
        A <= C < B   -> risky (위험)
        C < A        -> impossible (불가능)

    A = required_min_minutes (모든 작업의 최소 예상시간 합계)
    B = required_max_minutes (모든 작업의 최대 예상시간 합계)
    C = available_minutes (모든 가용시간 합계)

    Returns:
        {
            "status": "possible" | "risky" | "impossible",
            "required_min_minutes": int,
            "required_recommended_minutes": int,
            "available_minutes": int,
            "shortage_minutes": int,  # possible이면 0
        }
    """
    if available_minutes >= required_max_minutes:
        status = POSSIBLE
        shortage_minutes = 0
    elif available_minutes >= required_min_minutes:
        status = RISKY
        shortage_minutes = required_max_minutes - available_minutes
    else:
        status = IMPOSSIBLE
        shortage_minutes = required_min_minutes - available_minutes

    return {
        "status": status,
        "required_min_minutes": required_min_minutes,
        "required_recommended_minutes": required_max_minutes,
        "available_minutes": available_minutes,
        "shortage_minutes": shortage_minutes,
    }
