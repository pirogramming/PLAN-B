"""
BE3 담당 - AI 분석과 예상시간 계산을 이어붙이는 오케스트레이션 계층.

여기 있는 이유:
- exams.services.task_extractor: AI 분석만 담당 (estimated_min/max_minutes는 0)
- planner.services.time_estimator: 예상시간 계산 "방법"만 담당 (BE1 소유, 순수 함수)
- 이 둘을 어떤 순서로 이어붙일지는 어느 한쪽 파일의 책임이 아니라서 별도로 분리했다.

이렇게 분리한 이유:
- task_extractor.py가 planner 앱을 몰라도 되게 유지 (앱 경계 유지)
- "AI 분석 → 예상시간 계산" 세트를 부르는 진입점(E-AI-01 최초 분석, E-AI-03 재분석)이
  2곳 이상이라, View에서 매번 두 함수를 따로 호출하면 중복·누락 위험이 있다.
  이 함수 하나로 그 위험을 없앤다.
"""
from __future__ import annotations

import logging

from django.db import transaction

from exams.models import StudyMaterial, StudyTask
from exams.services.task_extractor import analyze_study_material
from planner.services.time_estimator import estimate_task_minutes

logger = logging.getLogger(__name__)


@transaction.atomic
def analyze_and_estimate(study_material: StudyMaterial) -> list[StudyTask]:
    """
    AI 분석(task_extractor)과 예상시간 계산(time_estimator)을 순서대로 실행한다.

    처리 순서:
        1. analyze_study_material()로 StudyTask 생성 (estimated_min/max_minutes=0)
        2. 생성된 각 StudyTask에 대해 BE1의 estimate_task_minutes() 호출
        3. 계산된 예상시간을 한 번에 bulk_update로 반영

    실패 시:
        - 1단계 실패: task_extractor의 예외(AICallFailedError, AIResponseValidationError)가
          그대로 전파되고, StudyTask는 생성되지 않는다 (task_extractor 자체가 원자적).
        - 2단계 실패: 이 함수 전체가 @transaction.atomic이므로 1단계에서 만든
          StudyTask도 함께 롤백된다. "예상시간 없는 StudyTask"가 DB에 남지 않는다.
    """
    tasks = analyze_study_material(study_material)

    if not tasks:
        return tasks

    exam = study_material.exam
    for task in tasks:
        est_min, est_max = estimate_task_minutes(
            task_type=task.task_type,
            difficulty=task.difficulty,
            speed_factor=exam.speed_factor,
        )
        task.estimated_min_minutes = est_min
        task.estimated_max_minutes = est_max

    StudyTask.objects.bulk_update(
        tasks, ["estimated_min_minutes", "estimated_max_minutes"]
    )

    logger.info(
        "AI 분석 + 예상시간 계산 완료: exam=%s, 작업 %d개",
        exam.subject_name, len(tasks),
    )
    return tasks