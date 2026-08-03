"""
BE3 담당 - AI 분석과 예상시간 계산을 이어붙이는 오케스트레이션 계층.
(기능 명세서 E-AI-01/02/03 대응)

여기 있는 이유:
- exams.services.task_extractor: AI 분석만 담당 (estimated_min/max_minutes는 0)
- planner.services.time_estimator: 예상시간 계산 "방법"만 담당 (BE1 소유, 순수 함수)
- 이 둘을 어떤 순서로 이어붙일지, StudyMaterial의 AI 분석 진행 상태를 어떻게
  갱신할지는 어느 한쪽 파일의 책임이 아니라서 별도로 분리했다.

이렇게 분리한 이유:
- task_extractor.py가 planner 앱을 몰라도 되게 유지 (앱 경계 유지)
- "AI 분석 → 예상시간 계산" 세트를 부르는 진입점(E-AI-01 최초 분석, E-AI-03 재분석)이
  2곳 이상이라, View에서 매번 두 함수를 따로 호출하면 중복·누락 위험이 있다.
  이 함수 하나로 그 위험을 없앤다.

상태 필드 설계 (리뷰 확정 사항):
- StudyMaterial.status / error_message: PDF·텍스트 "추출" 상태 전용 (건드리지 않음)
- StudyMaterial.analysis_status / analysis_error_message / analysis_retry_count:
  AI "분석" 상태 전용 (이 파일이 관리)

분석 상태 전이 (리뷰 확정 사항):
- 최초 분석: PENDING -> PROCESSING -> COMPLETED 또는 FAILED
- 재시도:   FAILED   -> PROCESSING -> COMPLETED 또는 FAILED
- PROCESSING 상태에서는 중복 분석 요청을 거부한다.
- COMPLETED 상태에서는 MVP 기준 재분석을 지원하지 않는다
  (결과를 고치고 싶으면 AI 재분석이 아니라 사용자가 작업 검토 화면에서 직접 수정한다).
- 사용자 재시도는 최대 2회. 최초 분석은 이 횟수에 포함하지 않는다.
  (최초 분석 실패 -> retry_count=0, 1차 재시도 시작 -> 1, 2차 재시도 시작 -> 2,
   2차까지 실패하면 추가 재시도 거부하고 직접 입력 화면으로 안내)
- task_extractor 내부의 JSON 검증 self-correction 재요청은 이 재시도 횟수에 포함하지 않는다
  (그건 AI 응답 하나를 받는 과정의 내부 디테일이지, 사용자가 누른 "재시도"가 아니다).
- AI 분석 결과 StudyTask가 0개 생성되면 성공으로 보지 않고 FAILED로 처리한다.

동시 요청/중복 방지:
- "PENDING인지 확인 -> PROCESSING으로 저장" 을 두 단계로 나누면 동시 요청 사이에
  경쟁 상태(race condition)가 생길 수 있다. 그래서 확인과 전이를 DB 조건부
  UPDATE(QuerySet.filter().update()) 하나로 원자적으로 처리한다: 이 UPDATE가
  실제로 영향을 준 행(row)이 0개면 "지금은 시작할 수 없는 상태"라고 판단한다.
- 재시도 시작 시 analysis_retry_count 증가도 F() 표현식으로 원자적으로 처리한다.

예외 처리 범위:
- task_extractor/time_estimator/bulk_update 등 파이프라인 전체에서 발생하는
  모든 예외를 잡아 analysis_status=FAILED로 남긴다.
  - AIAnalysisError 계열: 사용자에게 보여줘도 되는 실패 사유를 그대로 저장
  - 그 외 예기치 못한 예외: 상세 내용은 로그에만 남기고, 사용자용 메시지는
    일반적인 문구로 저장 (내부 구현 노출 방지)

알려진 한계 (이번 PR 범위 밖, 후속 리팩터링 이슈로 분리):
- task_extractor.analyze_study_material()가 @transaction.atomic이라, 그 안에서
  이뤄지는 AI 네트워크 호출이 DB 트랜잭션을 물고 있는 상태로 실행된다.
  외부 네트워크 호출을 트랜잭션 안에 두는 것은 이상적이지 않지만, task_extractor
  구조 자체를 바꿔야 하는 사안이라 이 PR에서는 다루지 않는다.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.db.models import F

from core.choices import MaterialStatus
from core.exceptions import AIAnalysisError, AIResponseValidationError
from exams.models import StudyMaterial, StudyTask
from exams.services.task_extractor import analyze_study_material
from planner.services.time_estimator import estimate_task_minutes

logger = logging.getLogger(__name__)

MAX_RETRY_COUNT = 2


class DuplicateAnalysisRequestError(Exception):
    """이미 처리 중이거나(PROCESSING), 지금 상태에서는 분석/재시도를 시작할 수 없을 때"""


class RetryLimitExceededError(Exception):
    """사용자 재시도 횟수(MAX_RETRY_COUNT)를 이미 다 쓴 상태에서 또 재시도를 요청했을 때"""


class AnalysisNotSupportedError(Exception):
    """COMPLETED 상태처럼, 정책상 이 상태에서는 (재)분석을 지원하지 않을 때"""


class AnalysisPipelineError(Exception):
    """
    분석 파이프라인(task_extractor/time_estimator/bulk_update 등)에서
    AIAnalysisError가 아닌 예기치 못한 예외가 발생했을 때 이 타입으로 변환해서 던진다.

    View 등 호출부가 "AIAnalysisError만 알면 되는" 상태를 유지할 수 있도록,
    예상 가능한 실패(AIAnalysisError)와 예상 못한 실패를 이 예외 하나로
    구분 없이 잡을 수 있게 한다. analysis_status=FAILED와 사용자용 일반
    오류 메시지는 이 예외가 발생하기 전에 이미 저장이 끝난 상태이며,
    이 예외의 메시지 자체도 사용자에게 그대로 노출해도 안전한 일반 문구다
    (내부 예외의 상세 내용/스택트레이스는 로그에만 남긴다).
    """


@transaction.atomic
def _run_analysis_and_estimate(study_material: StudyMaterial) -> list[StudyTask]:
    """
    AI 분석(task_extractor)과 예상시간 계산(time_estimator)을 순서대로 실행한다.
    analysis_status는 건드리지 않는다 (상태 관리는 호출하는 쪽이 담당).

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


def _finish_success(study_material: StudyMaterial, tasks: list[StudyTask]) -> None:
    study_material.analysis_status = MaterialStatus.COMPLETED
    study_material.analysis_error_message = None
    study_material.save(update_fields=["analysis_status", "analysis_error_message"])
    logger.info(
        "AI 분석 + 예상시간 계산 완료: exam=%s, 작업 %d개",
        study_material.exam.subject_name, len(tasks),
    )


def _finish_failure(study_material: StudyMaterial, message: str) -> None:
    study_material.analysis_status = MaterialStatus.FAILED
    study_material.analysis_error_message = message
    study_material.save(update_fields=["analysis_status", "analysis_error_message"])


def _execute_analysis(study_material: StudyMaterial) -> list[StudyTask]:
    """
    실제 분석+예상시간 계산을 실행하고 결과에 따라 analysis_status를
    COMPLETED 또는 FAILED로 마무리한다.

    호출 전제: analysis_status는 이미 PROCESSING으로 전이되어 있어야 한다
    (전이는 analyze_and_estimate/retry_analysis에서 원자적으로 처리한다).

    예외 처리:
        - AIAnalysisError 계열: 실패 사유를 그대로 analysis_error_message에 저장
        - 그 외 예기치 못한 예외: 상세는 로그에만 남기고, 사용자용 메시지는
          일반적인 문구로 저장 (내부 구현/스택트레이스 노출 방지)
        - StudyTask가 0개 생성된 경우: 성공으로 보지 않고 FAILED 처리
    """
    try:
        tasks = _run_analysis_and_estimate(study_material)
    except AIAnalysisError as exc:
        _finish_failure(study_material, str(exc))
        logger.warning(
            "AI 분석 실패: study_material_id=%s, 사유=%s", study_material.id, exc,
        )
        raise
    except Exception as exc:
        logger.exception(
            "AI 분석 파이프라인에서 예기치 못한 오류: study_material_id=%s",
            study_material.id,
        )
        message = "분석 중 알 수 없는 오류가 발생했습니다. 잠시 후 다시 시도해주세요."
        _finish_failure(study_material, message)
        raise AnalysisPipelineError(message) from exc

    if not tasks:
        message = "분석 결과 학습 작업이 생성되지 않았습니다."
        _finish_failure(study_material, message)
        logger.warning(
            "AI 분석 결과 0개: study_material_id=%s", study_material.id,
        )
        raise AIResponseValidationError(message)

    _finish_success(study_material, tasks)
    return tasks


def _start_processing(study_material: StudyMaterial, *, is_retry: bool) -> bool:
    """
    analysis_status를 PROCESSING으로 원자적으로 전이시킨다.

    - is_retry=False (최초 분석): 현재 analysis_status가 PENDING일 때만 전이
    - is_retry=True  (재시도):    현재 analysis_status가 FAILED이고
                                  analysis_retry_count < MAX_RETRY_COUNT일 때만 전이,
                                  전이와 동시에 analysis_retry_count를 1 증가시킨다.

    DB 조건부 UPDATE 하나로 "확인 + 변경"을 원자적으로 처리하기 때문에,
    동시에 같은 요청이 여러 번 들어와도 정확히 하나만 성공한다.

    Returns:
        True: 전이에 성공함 (study_material 인스턴스도 최신값으로 갱신됨)
        False: 조건이 안 맞아 전이하지 못함 (이미 처리중/조건 불충족 등)
    """
    if is_retry:
        updated_count = StudyMaterial.objects.filter(
            pk=study_material.pk,
            analysis_status=MaterialStatus.FAILED,
            analysis_retry_count__lt=MAX_RETRY_COUNT,
        ).update(
            analysis_status=MaterialStatus.PROCESSING,
            analysis_error_message=None,
            analysis_retry_count=F("analysis_retry_count") + 1,
        )
    else:
        updated_count = StudyMaterial.objects.filter(
            pk=study_material.pk,
            analysis_status=MaterialStatus.PENDING,
        ).update(
            analysis_status=MaterialStatus.PROCESSING,
            analysis_error_message=None,
        )

    if updated_count:
        study_material.refresh_from_db(
            fields=["analysis_status", "analysis_error_message", "analysis_retry_count"]
        )
        return True
    return False


def analyze_and_estimate(study_material: StudyMaterial) -> list[StudyTask]:
    """
    E-AI-01 진입점. analysis_status가 PENDING일 때만 분석을 시작한다.

    Raises:
        DuplicateAnalysisRequestError: PENDING이 아니어서(이미 진행/완료/실패) 시작 못 함
        AIAnalysisError 계열, AIResponseValidationError: 분석 자체가 실패함
    """
    started = _start_processing(study_material, is_retry=False)
    if not started:
        study_material.refresh_from_db(fields=["analysis_status"])
        raise DuplicateAnalysisRequestError(
            f"분석을 시작할 수 없는 상태입니다 (현재 analysis_status: "
            f"{study_material.analysis_status})."
        )
    return _execute_analysis(study_material)


def retry_analysis(study_material: StudyMaterial) -> list[StudyTask]:
    """
    E-AI-03 진입점. FAILED 상태이고 재시도 횟수가 남아있을 때만 재시도한다.

    Raises:
        DuplicateAnalysisRequestError: 현재 PROCESSING이라 중복 요청인 경우
        AnalysisNotSupportedError: COMPLETED 상태라 MVP 기준 재분석 미지원인 경우
        RetryLimitExceededError: FAILED 상태이지만 재시도 횟수(2회)를 이미 다 쓴 경우
        AIAnalysisError 계열, AIResponseValidationError: 재시도한 분석 자체가 실패함
    """
    started = _start_processing(study_material, is_retry=True)
    if not started:
        study_material.refresh_from_db(fields=["analysis_status", "analysis_retry_count"])
        status = study_material.analysis_status

        if status == MaterialStatus.PROCESSING:
            raise DuplicateAnalysisRequestError("이미 분석 중인 자료는 다시 분석할 수 없습니다.")
        if status == MaterialStatus.COMPLETED:
            raise AnalysisNotSupportedError(
                "이미 분석이 완료된 자료입니다. MVP에서는 재분석을 지원하지 않습니다. "
                "결과를 수정하려면 작업 검토 화면에서 직접 수정해주세요."
            )
        if status == MaterialStatus.FAILED:
            # FAILED인데도 전이 실패했다는 건 재시도 횟수를 이미 다 썼다는 뜻
            raise RetryLimitExceededError(
                "재시도 횟수(최대 2회)를 모두 사용했습니다. 학습 작업을 직접 추가해주세요."
            )
        raise DuplicateAnalysisRequestError(
            f"재시도할 수 없는 상태입니다 (현재 analysis_status: {status})."
        )

    return _execute_analysis(study_material)


def get_analysis_status(study_material: StudyMaterial) -> dict:
    """
    E-AI-02: StudyMaterial의 AI 분석 진행 상태를 조회한다.

    Returns:
        {
            "status": "pending" | "processing" | "completed" | "failed",
            "error_message": str | None,       # FAILED가 아니면 항상 None
            "retry_count": int,                # 지금까지 사용자가 재시도한 횟수
            "retry_remaining": int,             # 남은 재시도 가능 횟수 (0~2)
        }
    """
    return {
        "status": study_material.analysis_status,
        "error_message": (
            study_material.analysis_error_message
            if study_material.analysis_status == MaterialStatus.FAILED
            else None
        ),
        "retry_count": study_material.analysis_retry_count,
        "retry_remaining": max(0, MAX_RETRY_COUNT - study_material.analysis_retry_count),
    }