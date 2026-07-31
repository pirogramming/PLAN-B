"""
BE3 담당 - AI 분석과 예상시간 계산을 이어붙이는 오케스트레이션 계층.
(기능 명세서 E-AI-01/02/03 대응)

여기 있는 이유:
- exams.services.task_extractor: AI 분석만 담당 (estimated_min/max_minutes는 0)
- planner.services.time_estimator: 예상시간 계산 "방법"만 담당 (BE1 소유, 순수 함수)
- 이 둘을 어떤 순서로 이어붙일지, StudyMaterial의 분석 진행 상태(status)를 어떻게
  갱신할지는 어느 한쪽 파일의 책임이 아니라서 별도로 분리했다.

이렇게 분리한 이유:
- task_extractor.py가 planner 앱을 몰라도 되게 유지 (앱 경계 유지)
- "AI 분석 → 예상시간 계산" 세트를 부르는 진입점(E-AI-01 최초 분석, E-AI-03 재분석)이
  2곳 이상이라, View에서 매번 두 함수를 따로 호출하면 중복·누락 위험이 있다.
  이 함수 하나로 그 위험을 없앤다.

상태 갱신 설계(E-AI-02용):
- PENDING -> PROCESSING -> COMPLETED 또는 FAILED
- 상태 갱신은 StudyTask 생성과 별개의 트랜잭션으로 저장한다.
  (분석이 실패했을 때 "FAILED 상태 기록"까지 롤백되면 안 되기 때문 -
   전체를 하나의 @transaction.atomic으로 묶으면 실패 시 상태 변경 자체도 사라진다)
"""
from __future__ import annotations

import logging

from django.db import transaction

from core.choices import MaterialStatus
from core.exceptions import AIAnalysisError
from exams.models import StudyMaterial, StudyTask
from exams.services.task_extractor import analyze_study_material
from planner.services.time_estimator import estimate_task_minutes

logger = logging.getLogger(__name__)


@transaction.atomic
def _run_analysis_and_estimate(study_material: StudyMaterial) -> list[StudyTask]:
    """
    AI 분석(task_extractor)과 예상시간 계산(time_estimator)을 순서대로 실행한다.
    StudyMaterial.status는 건드리지 않는다 (상태 관리는 analyze_and_estimate가 담당).

    처리 순서:
        1. analyze_study_material()로 StudyTask 생성 (estimated_min/max_minutes=0)
        2. 생성된 각 StudyTask에 대해 BE1의 estimate_task_minutes() 호출
        3. 계산된 예상시간을 한 번에 bulk_update로 반영

    실패 시 이 함수 전체가 @transaction.atomic이므로 생성된 StudyTask도 함께
    롤백된다. "예상시간 없는 StudyTask"가 DB에 남지 않는다.
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
    return tasks


def analyze_and_estimate(study_material: StudyMaterial) -> list[StudyTask]:
    """
    E-AI-01 진입점. StudyMaterial.status를 PROCESSING -> COMPLETED/FAILED로
    갱신하면서 분석+예상시간 계산을 실행한다.

    성공: status=COMPLETED, error_message=None, 생성된 StudyTask 목록 반환
    실패: status=FAILED, error_message에 실패 사유 저장 후 예외를 그대로 재발생
    """
    study_material.status = MaterialStatus.PROCESSING
    study_material.error_message = None
    study_material.save(update_fields=["status", "error_message"])

    try:
        tasks = _run_analysis_and_estimate(study_material)
    except AIAnalysisError as exc:
        study_material.status = MaterialStatus.FAILED
        study_material.error_message = str(exc)
        study_material.save(update_fields=["status", "error_message"])
        logger.warning(
            "AI 분석 실패: study_material_id=%s, 사유=%s",
            study_material.id, exc,
        )
        raise

    study_material.status = MaterialStatus.COMPLETED
    study_material.save(update_fields=["status"])

    logger.info(
        "AI 분석 + 예상시간 계산 완료: exam=%s, 작업 %d개",
        study_material.exam.subject_name, len(tasks),
    )
    return tasks


def get_analysis_status(study_material: StudyMaterial) -> dict:
    """
    E-AI-02: StudyMaterial의 분석 진행 상태를 조회한다.

    Returns:
        {
            "status": "pending" | "processing" | "completed" | "failed",
            "error_message": str | None,  # FAILED가 아니면 항상 None
        }
    """
    return {
        "status": study_material.status,
        "error_message": (
            study_material.error_message
            if study_material.status == MaterialStatus.FAILED
            else None
        ),
    }


def retry_analysis(study_material: StudyMaterial) -> list[StudyTask]:
    """
    E-AI-03: 분석에 실패(FAILED)한 StudyMaterial을 다시 분석한다.

    예외 처리 (명세서 기준):
        - 이미 분석 중(PROCESSING)인 자료는 중복 분석하지 않는다.
        - COMPLETED 상태는 사용자 확인 없이 재분석하지 않는다.
          (재분석을 원하면 호출하는 쪽에서 사용자 확인을 받은 뒤,
           별도 "강제 재분석" 경로를 쓰거나 상태를 직접 조정해야 한다 - 이 함수는
           오직 FAILED -> 재시도 경로만 허용한다)

    주의:
        - "재시도 횟수 초과 시 직접 텍스트 입력 안내"는 명세서에 있으나,
          현재 StudyMaterial 모델에 재시도 횟수를 저장하는 필드가 없어 이 함수는
          아직 구현하지 않는다. 필드 추가가 필요하면 팀 확인 후 별도 작업으로 진행.
    """
    if study_material.status == MaterialStatus.PROCESSING:
        raise ValueError("이미 분석 중인 자료는 다시 분석할 수 없습니다.")
    if study_material.status == MaterialStatus.COMPLETED:
        raise ValueError(
            "이미 분석이 완료된 자료입니다. 재분석하려면 사용자 확인이 필요합니다."
        )
    if study_material.status != MaterialStatus.FAILED:
        raise ValueError(f"재시도할 수 없는 상태입니다: {study_material.status}")

    return analyze_and_estimate(study_material)