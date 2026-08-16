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
- COMPLETED 상태에서는 MVP 기준 재분석을 지원하지 않는다.
- 사용자 재시도는 최대 2회. 최초 분석은 이 횟수에 포함하지 않는다.
- task_extractor 내부의 JSON 검증 self-correction 재요청은 이 재시도 횟수에 포함하지 않는다.
- AI 분석 결과 StudyTask가 0개 생성되면 성공으로 보지 않고 FAILED로 처리한다.

동시 요청/중복 방지:
- "확인 -> 저장"을 두 단계로 나누면 경쟁 상태가 생길 수 있어서, DB 조건부
  UPDATE(QuerySet.filter().update()) 하나로 원자적으로 처리한다.
- 재시도 시작 시 analysis_retry_count 증가도 F() 표현식으로 원자적으로 처리한다.

예외 처리 범위:
- task_extractor/time_estimator/bulk_update 등 파이프라인 전체에서 발생하는
  모든 예외를 잡아 analysis_status=FAILED로 남긴다.

AI 호출과 DB 저장 사이의 입력 변경 경쟁 상태:
- AI 네트워크 호출을 트랜잭션 밖으로 뺀 대가로, "AI 호출 시작 ~ 결과 저장" 사이에
  StudyMaterial이 바뀔 수 있는 창(window)이 생겼다. _save_tasks_with_estimates()가
  저장 직전에 StudyMaterial을 다시 조회해서 analysis_status/status(텍스트 추출 상태)/
  extracted_text가 AI 호출 당시와 같은지 재검증하고, 어긋나면 StaleAnalysisRequestError를
  던지고 아무것도 저장하지 않는다. 예상시간 계산에 쓰는 exam.speed_factor도 저장
  시점의 최신 값을 쓴다. material_extract()(BE2 담당) 쪽에도 analysis_status가
  PROCESSING/COMPLETED인 자료의 재추출을 조건부 UPDATE로 막는 대칭 방어가 있다.

PDF 추출 시작과 AI 분석 시작이 동시에 성공하는 경쟁 상태:
- _start_processing()의 조건부 UPDATE에 status=MaterialStatus.COMPLETED 조건을
  추가해서, material_extract() 쪽 방어와 서로 대칭을 이루게 했다. 두 요청 중 DB에
  먼저 도달해 조건부 UPDATE를 통과한 쪽만 성공하고 나머지는 원자적으로 실패한다.

PROCESSING 타임아웃 / 좀비 상태 복구 (이슈 #52):
- 서버가 분석 도중 비정상 종료되면 analysis_status가 PROCESSING인 채로 영원히
  남을 수 있다. analysis_started_at 기준 PROCESSING_TIMEOUT_SECONDS(5분) 이상
  지났거나, analysis_started_at이 NULL인(이 필드가 생기기 전부터 PROCESSING이었던
  기존 데이터) PROCESSING은 "좀비"로 간주한다.
- 정책 단순화: 좀비 구제는 retry_analysis()에서만 허용한다.
  analyze_and_estimate()는 status=COMPLETED, analysis_status=PENDING일 때만
  시작한다 (좀비 구제 없음). 화면에서 is_stale=True일 때도 재시도 엔드포인트로
  안내하면 된다.
- 재시도 횟수 제한: 좀비 상태여도 analysis_retry_count < MAX_RETRY_COUNT 조건은
  동일하게 적용한다. eligible 조건 전체에 이 조건을 AND로 묶어서, 좀비라는
  이유로 재시도 횟수 제한을 우회할 수 없게 했다.
- NULL 처리: analysis_started_at이 NULL인 PROCESSING도 좀비로 취급해서 구제
  대상에 포함한다. 별도 데이터 마이그레이션 없이 이 판정 로직만으로 처리한다.

실행 소유권(analysis_run_id) - 좀비 복구 도입으로 생긴 새 문제와 해결책:
- 좀비 PROCESSING을 새 실행이 대신 이어받게 해주면, 원래 실행이 실제로는 죽지
  않고 뒤늦게 계속 진행 중이었을 경우 두 실행이 동시에 같은 StudyMaterial을
  건드리게 된다. 늦게 끝난 예전 실행이 새 실행의 결과(StudyTask, 상태)를
  덮어쓸 수 있다.
- 해결: PROCESSING으로 전이될 때마다 새로운 UUID(analysis_run_id)를 발급한다.
  그 이후의 모든 DB 쓰기(StudyTask 저장, 상태 완료/실패 처리)는 그 시점의
  analysis_run_id가 자신이 발급받은 값과 여전히 같은지 확인한 뒤에만 수행한다.
  다르면(이미 다른 실행이 이어받았다면) 조용히 포기한다 (StaleAnalysisRunError).
  이 확인은 StaleAnalysisRequestError(입력이 바뀐 경우) 확인과는 별개다 - 실행
  소유권을 잃은 경우엔 이미 다른 실행이 상태를 관리하고 있으므로, 이 실행은
  analysis_status를 아예 건드리지 않고 조용히 물러난다.
"""
from __future__ import annotations

import logging
import uuid

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from core.choices import MaterialStatus
from core.exceptions import AIAnalysisError, AIResponseValidationError
from exams.models import StudyMaterial, StudyTask
from exams.services.task_extractor import fetch_extracted_tasks, save_extracted_tasks
from planner.services.time_estimator import estimate_task_minutes

logger = logging.getLogger(__name__)

MAX_RETRY_COUNT = 2

# PROCESSING 상태가 이 시간(초)보다 오래 지속되면 "좀비 상태"로 간주하고,
# 재시도 요청이 이 자리를 대신 차지할 수 있게 허용한다.
PROCESSING_TIMEOUT_SECONDS = 300  # 5분


class DuplicateAnalysisRequestError(Exception):
    """이미 처리 중이거나(PROCESSING), 지금 상태에서는 분석/재시도를 시작할 수 없을 때"""


class RetryLimitExceededError(Exception):
    """사용자 재시도 횟수(MAX_RETRY_COUNT)를 이미 다 쓴 상태에서 또 재시도를 요청했을 때"""


class AnalysisNotSupportedError(Exception):
    """COMPLETED 상태처럼, 정책상 이 상태에서는 (재)분석을 지원하지 않을 때"""


class StaleAnalysisRequestError(Exception):
    """
    AI 호출 시작 이후 저장 시점까지 사이에 StudyMaterial의 입력이 바뀌어서
    (analysis_status가 더 이상 PROCESSING이 아니거나, status가 COMPLETED가
    아니거나, extracted_text가 AI 호출 당시와 달라져서) 지금 들고 있는 AI
    결과를 더 이상 신뢰할 수 없을 때. 이 실행은 여전히 PROCESSING의 소유자이므로
    (analysis_run_id 기준), 안전하게 FAILED로 마무리해 재시도를 안내한다.
    """


class StaleAnalysisRunError(Exception):
    """
    이 실행(analysis_run_id)이 결과를 저장하기 전에 이미 다른(더 최신) 실행이
    같은 StudyMaterial의 소유권을 가져간 경우. 이 경우 결과는 저장하지 않고
    조용히 포기한다 - 이미 다른 실행이 상태를 관리 중이므로 analysis_status를
    건드리면 안 된다.
    """


class AnalysisPipelineError(Exception):
    """
    분석 파이프라인(task_extractor/time_estimator/bulk_update 등)에서
    AIAnalysisError가 아닌 예기치 못한 예외가 발생했을 때 이 타입으로 변환해서 던진다.

    View 등 호출부가 "AIAnalysisError만 알면 되는" 상태를 유지할 수 있도록,
    예상 가능한 실패(AIAnalysisError)와 예상 못한 실패를 이 예외 하나로
    구분 없이 잡을 수 있게 한다.
    """


def _run_analysis_and_estimate(study_material: StudyMaterial, run_id: uuid.UUID) -> list[StudyTask]:
    """
    AI 분석(task_extractor)과 예상시간 계산(time_estimator)을 순서대로 실행한다.
    analysis_status는 건드리지 않는다 (상태 관리는 호출하는 쪽이 담당).

    run_id: 이 실행을 시작할 때 _start_processing()이 발급한 실행 식별자.
    DB에 쓰기 직전에 이 값이 여전히 유효한 "현재 실행"인지 확인한다
    (_save_tasks_with_estimates 참고).
    """
    if not study_material.extracted_text:
        raise AIResponseValidationError("StudyMaterial에 분석할 텍스트가 없습니다.")

    exam = study_material.exam
    analyzed_text = study_material.extracted_text
    extracted_tasks = fetch_extracted_tasks(exam, analyzed_text)

    return _save_tasks_with_estimates(study_material, extracted_tasks, analyzed_text, run_id)


@transaction.atomic
def _save_tasks_with_estimates(
    study_material: StudyMaterial, extracted_tasks, analyzed_text: str, run_id: uuid.UUID
) -> list[StudyTask]:
    """
    AI가 추출한 결과를 StudyTask로 저장하고, 곧바로 예상시간까지 채운 뒤,
    analysis_status=COMPLETED 최종 전이까지 전부 같은 트랜잭션 안에서 처리한다.

    저장 직전에 StudyMaterial을 다시 조회해서(select_for_update로 잠그면서) 4가지를
    재검증한다:
        1. analysis_run_id가 여전히 이 실행(run_id)의 것인지 (실행 소유권)
        2. analysis_status가 여전히 PROCESSING인지
        3. status(텍스트 추출 상태)가 COMPLETED인지
        4. extracted_text가 analyzed_text(AI 호출에 실제로 쓴 텍스트)와 같은지
    1번이 어긋나면 StaleAnalysisRunError(이미 다른 실행이 선점), 나머지는
    StaleAnalysisRequestError(입력이 바뀜)를 던지고 아무것도 쓰지 않는다.

    예상시간 계산에 쓰는 exam.speed_factor도 이 재조회로 얻은 최신 값을 사용한다.

    리뷰 반영(#84): 원래는 이 함수가 StudyTask 저장만 하고 커밋한 뒤,
    _finish_success()가 별도 트랜잭션으로 analysis_status=COMPLETED 전이를 했다.
    그 사이(저장 커밋 ~ 완료 전이 사이)에 다른 실행이 소유권을 가져가면,
    "StaleAnalysisRunError는 정상적으로 전파되지만, 이미 저장된 StudyTask는
    롤백되지 않고 그대로 남는" 데이터 정합성 문제가 있었다 - 예를 들어 그
    다른 실행이 이어서 AI 호출 단계에서 실패해 자기 결과를 저장하는 데까지
    못 갔다면, 최종 analysis_status는 FAILED인데 이전 실행이 만든 StudyTask가
    남아서 material_detail에서 그대로 노출될 수 있었다.

    이제는 select_for_update()로 잠근 행을 트랜잭션이 끝날 때까지 계속 들고
    있으면서, 저장과 최종 완료 전이를 같은 트랜잭션에 묶는다. 이 트랜잭션이
    끝나기 전까지 다른 트랜잭션은 이 행을 갱신하는 UPDATE에서 대기하게 되므로,
    최종 전이 시점에도 소유권이 그대로 보존된다. 혹시라도 최종 조건부 UPDATE가
    실패하면(방어적으로 여전히 확인한다) StaleAnalysisRunError를 던져서 트랜잭션
    전체를 롤백시킨다 - StudyTask 저장까지 같이 취소되어 고아 데이터가 남지 않는다.
    AI 네트워크 호출(fetch_extracted_tasks)은 이미 이 함수 밖에서 끝난 뒤이므로,
    이 트랜잭션 동안 네트워크 호출로 DB 커넥션을 오래 점유하는 문제는 없다.
    """
    current = (
        StudyMaterial.objects
        .select_for_update()
        .select_related("exam")
        .get(pk=study_material.pk)
    )

    if current.analysis_run_id != run_id:
        raise StaleAnalysisRunError(
            f"분석 실행(run_id={run_id})이 결과를 저장하기 전에 다른 실행으로 "
            f"대체되어 결과를 저장하지 않습니다. study_material_id={study_material.pk}"
        )

    if current.analysis_status != MaterialStatus.PROCESSING:
        raise StaleAnalysisRequestError(
            f"저장 시점에 analysis_status가 PROCESSING이 아닙니다 "
            f"(현재: {current.analysis_status}). study_material_id={study_material.pk}"
        )

    if current.status != MaterialStatus.COMPLETED:
        raise StaleAnalysisRequestError(
            f"저장 시점에 텍스트 추출 상태가 COMPLETED가 아닙니다 "
            f"(현재: {current.status}). study_material_id={study_material.pk}"
        )

    if current.extracted_text != analyzed_text:
        raise StaleAnalysisRequestError(
            f"저장 시점에 추출 텍스트가 AI 호출 당시와 달라 결과를 저장하지 않습니다. "
            f"study_material_id={study_material.pk}"
        )

    tasks = save_extracted_tasks(current, extracted_tasks)

    if not tasks:
        # 빈 결과는 이 함수 책임이 아니라 _execute_analysis()가 FAILED로 마무리한다
        # (성공으로 볼 만한 게 없으니 COMPLETED 전이도 하지 않는다).
        return tasks

    exam = current.exam  # 재조회로 얻은 최신 exam (speed_factor 최신값 보장)
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

    # StudyTask 저장과 같은 트랜잭션 안에서 최종 완료 전이까지 처리한다 (위 docstring
    # 참고). select_for_update()로 이 행을 계속 잠그고 있었으므로 이 시점에도
    # analysis_run_id가 run_id와 같다는 게 사실상 보장되지만, 방어적으로 조건부
    # UPDATE로 한 번 더 확인한다 - 실패하면 트랜잭션 전체(StudyTask 저장 포함)가
    # 롤백된다.
    updated_count = StudyMaterial.objects.filter(
        pk=study_material.pk, analysis_run_id=run_id,
    ).update(analysis_status=MaterialStatus.COMPLETED, analysis_error_message=None)

    if not updated_count:
        raise StaleAnalysisRunError(
            f"실행(run_id={run_id})이 StudyTask 저장과 같은 트랜잭션 안에서 최종 완료 "
            f"전이를 시도했지만 이미 다른 실행으로 대체되었습니다. "
            f"study_material_id={study_material.pk}"
        )

    study_material.analysis_status = MaterialStatus.COMPLETED
    study_material.analysis_error_message = None
    logger.info(
        "AI 분석 + 예상시간 계산 완료: exam=%s, 작업 %d개",
        exam.subject_name, len(tasks),
    )
    return tasks

def _finish_failure(study_material: StudyMaterial, message: str, run_id: uuid.UUID) -> None:
    """
    run_id가 여전히 "현재 실행"일 때만 FAILED로 갱신한다 (_finish_success와 동일한 이유).

    리뷰 반영(#84): _finish_success()와 대칭으로, 소유권을 잃었으면 조용히 넘어가는
    대신 StaleAnalysisRunError를 던진다. 이렇게 하면 호출부(_execute_analysis)가
    "이 실행이 실패했다"는 원래 예외 대신 "이미 다른 실행에 넘어갔다"는 사실을
    사용자에게 전달할 수 있다 - 안 그러면 이미 다른 실행이 성공적으로 처리
    중이거나 처리를 마쳤을 수도 있는데, 사용자는 "실패했다"는 오래된(stale) 메시지를
    보게 된다.
    """
    updated_count = StudyMaterial.objects.filter(
        pk=study_material.pk, analysis_run_id=run_id,
    ).update(analysis_status=MaterialStatus.FAILED, analysis_error_message=message)

    if not updated_count:
        logger.warning(
            "실행(run_id=%s)이 실패했지만 이미 다른 실행으로 대체되어 "
            "상태 갱신을 건너뜁니다: study_material_id=%s", run_id, study_material.id,
        )
        raise StaleAnalysisRunError(
            f"실행(run_id={run_id})이 실패 처리 시점에 이미 다른 실행으로 "
            f"대체되었습니다. study_material_id={study_material.pk}"
        )

    study_material.analysis_status = MaterialStatus.FAILED
    study_material.analysis_error_message = message


def _execute_analysis(study_material: StudyMaterial, run_id: uuid.UUID) -> list[StudyTask]:
    """
    실제 분석+예상시간 계산을 실행하고 결과에 따라 analysis_status를
    COMPLETED 또는 FAILED로 마무리한다.

    호출 전제: analysis_status는 이미 PROCESSING으로 전이되어 있고, run_id는
    그 전이 시점에 _start_processing()이 발급한 값이어야 한다.

    예외 처리:
        - StaleAnalysisRunError: 이미 다른 실행에게 선점당함. StudyTask 저장과
          analysis_status=COMPLETED 최종 전이가 이제 같은 트랜잭션으로 묶여있으므로
          (리뷰 반영 #84), 이 예외가 발생하면 그 트랜잭션 전체가 롤백되어 StudyTask
          저장도 함께 취소된다 - "저장은 됐는데 상태만 못 바뀐" 어중간한 상태가
          남지 않는다. analysis_status를 건드리지 않고(이미 그 다른 실행이 관리
          중이므로) 그대로 다시 던진다 - 이 경우 원래 실패하려던 사유
          (AIAnalysisError, StaleAnalysisRequestError 등)보다 우선한다.
        - StaleAnalysisRequestError: 입력이 바뀜. 이 실행은 여전히 소유자이므로
          안전하게 FAILED로 마무리한다.
        - AIAnalysisError 계열: 실패 사유를 그대로 analysis_error_message에 저장
        - 그 외 예기치 못한 예외: 상세는 로그에만 남기고, 사용자용 메시지는
          일반적인 문구로 저장
        - StudyTask가 0개 생성된 경우: 성공으로 보지 않고 FAILED 처리
    """
    try:
        tasks = _run_analysis_and_estimate(study_material, run_id)
    except StaleAnalysisRunError:
        logger.info(
            "실행(run_id=%s)이 결과 저장 전에 이미 다른 실행으로 대체됨: "
            "study_material_id=%s", run_id, study_material.id,
        )
        raise
    except StaleAnalysisRequestError as exc:
        message = "분석 도중 자료 내용이 변경되어 결과를 저장하지 않았습니다. 다시 시도해주세요."
        logger.warning(
            "분석 결과 저장 시점 재검증 실패: study_material_id=%s, 사유=%s",
            study_material.id, exc,
        )
        try:
            _finish_failure(study_material, message, run_id)
        except StaleAnalysisRunError:
            # 리뷰 반영(#84): _finish_failure() 자체도 소유권을 잃었다면, 이
            # 실행이 "실패했다"는 오래된 사실보다 "이미 다른 실행에 넘어갔다"는
            # 사실을 우선 전달한다 (_finish_success와 동일한 원칙).
            raise
        raise AnalysisPipelineError(message) from exc
    except AIAnalysisError as exc:
        logger.warning(
            "AI 분석 실패: study_material_id=%s, 사유=%s", study_material.id, exc,
        )
        try:
            _finish_failure(study_material, str(exc), run_id)
        except StaleAnalysisRunError:
            raise
        raise
    except Exception as exc:
        logger.exception(
            "AI 분석 파이프라인에서 예기치 못한 오류: study_material_id=%s",
            study_material.id,
        )
        message = "분석 중 알 수 없는 오류가 발생했습니다. 잠시 후 다시 시도해주세요."
        try:
            _finish_failure(study_material, message, run_id)
        except StaleAnalysisRunError:
            raise
        raise AnalysisPipelineError(message) from exc

    if not tasks:
        message = "분석 결과 학습 작업이 생성되지 않았습니다."
        logger.warning(
            "AI 분석 결과 0개: study_material_id=%s", study_material.id,
        )
        try:
            _finish_failure(study_material, message, run_id)
        except StaleAnalysisRunError:
            raise
        raise AIResponseValidationError(message)

    # _save_tasks_with_estimates()가 StudyTask 저장과 analysis_status=COMPLETED
    # 최종 전이까지 같은 트랜잭션 안에서 이미 끝냈다 (리뷰 반영 #84 - 저장과 완료
    # 전이 사이의 소유권 경쟁으로 StudyTask만 남고 상태는 다른 것으로 바뀌는
    # 데이터 정합성 문제를 막기 위함). tasks가 여기까지 정상 반환됐다는 것 자체가
    # 이미 COMPLETED 전이까지 성공했다는 뜻이므로, 별도로 완료 처리를 할 필요가 없다.
    return tasks


def _start_processing(study_material: StudyMaterial, *, is_retry: bool) -> uuid.UUID | None:
    """
    analysis_status를 PROCESSING으로 원자적으로 전이시키고, 성공하면 이번
    실행을 식별하는 새 UUID(run_id)를 발급해서 반환한다.

    - is_retry=False (최초 분석): status가 COMPLETED이고 analysis_status가
      PENDING일 때만 전이. 좀비 PROCESSING 구제는 여기서 하지 않는다
      (retry_analysis 전용 정책).
    - is_retry=True  (재시도): status가 COMPLETED이고, analysis_retry_count가
      MAX_RETRY_COUNT 미만이며, 아래 중 하나일 때 전이한다 (전이와 동시에
      retry_count를 1 증가시킨다).
        1) analysis_status가 FAILED
        2) analysis_status가 PROCESSING이고 좀비 상태
           (analysis_started_at이 PROCESSING_TIMEOUT_SECONDS 이상 지났거나 NULL)
      재시도 횟수 조건은 위 두 경우 모두에 동일하게 적용된다 (좀비라고 재시도
      횟수 제한을 우회할 수 없다).

    DB 조건부 UPDATE 하나로 "확인 + 변경"을 원자적으로 처리하기 때문에,
    동시에 같은 요청이 여러 번 들어와도 정확히 하나만 성공한다.

    Returns:
        성공 시 새로 발급된 run_id(uuid.UUID). 조건이 안 맞아 전이하지 못하면 None.
    """
    now = timezone.now()
    new_run_id = uuid.uuid4()
    stale_cutoff = now - timezone.timedelta(seconds=PROCESSING_TIMEOUT_SECONDS)

    if is_retry:
        is_zombie_processing = Q(analysis_status=MaterialStatus.PROCESSING) & (
            Q(analysis_started_at__lt=stale_cutoff) | Q(analysis_started_at__isnull=True)
        )
        eligible = (
            Q(status=MaterialStatus.COMPLETED)
            & Q(analysis_retry_count__lt=MAX_RETRY_COUNT)
            & (Q(analysis_status=MaterialStatus.FAILED) | is_zombie_processing)
        )
        updated_count = StudyMaterial.objects.filter(
            Q(pk=study_material.pk) & eligible
        ).update(
            analysis_status=MaterialStatus.PROCESSING,
            analysis_error_message=None,
            analysis_retry_count=F("analysis_retry_count") + 1,
            analysis_started_at=now,
            analysis_run_id=new_run_id,
        )
    else:
        updated_count = StudyMaterial.objects.filter(
            pk=study_material.pk,
            status=MaterialStatus.COMPLETED,
            analysis_status=MaterialStatus.PENDING,
        ).update(
            analysis_status=MaterialStatus.PROCESSING,
            analysis_error_message=None,
            analysis_started_at=now,
            analysis_run_id=new_run_id,
        )

    if updated_count:
        study_material.refresh_from_db(
            fields=[
                "analysis_status", "analysis_error_message",
                "analysis_retry_count", "analysis_started_at", "analysis_run_id",
            ]
        )
        return new_run_id
    return None


def claim_analysis_run(study_material: StudyMaterial, *, is_retry: bool) -> uuid.UUID:
    """
    analyze_and_estimate()/retry_analysis()의 앞부분(선점 단계)만 분리한 함수.
    ExamPeriod 락을 쥔 채로 호출해서, planner.plan_generate()가 같은 락
    기준으로 'PROCESSING 자료가 있는지'를 확인할 수 있게 한다
    (exams.views의 check_exam_period_not_locked_by_material_id 참고).

    _start_processing()과 동일한 원자적 조건부 UPDATE를 사용하며, 실패
    조건별로 기존 analyze_and_estimate()/retry_analysis()가 던지던 것과
    동일한 예외를 그대로 던진다. 성공하면 run_id만 반환하고, 실제 AI 호출
    (run_claimed_analysis)은 호출부가 락 밖에서 별도로 실행해야 한다.
    """
    run_id = _start_processing(study_material, is_retry=is_retry)
    if run_id is not None:
        return run_id

    study_material.refresh_from_db(
        fields=["status", "analysis_status", "analysis_retry_count", "analysis_started_at"]
    )

    if study_material.status != MaterialStatus.COMPLETED:
        action = "재시도" if is_retry else "분석"
        raise DuplicateAnalysisRequestError(
            f"텍스트 추출이 진행 중이라 지금은 {action}을 시작할 수 없습니다."
        )

    if not is_retry:
        raise DuplicateAnalysisRequestError(
            f"분석을 시작할 수 없는 상태입니다 (현재 analysis_status: "
            f"{study_material.analysis_status})."
        )

    status = study_material.analysis_status

    if status == MaterialStatus.COMPLETED:
        raise AnalysisNotSupportedError(
            "이미 분석이 완료된 자료입니다. MVP에서는 재분석을 지원하지 않습니다. "
            "결과를 수정하려면 작업 검토 화면에서 직접 수정해주세요."
        )

    if status == MaterialStatus.PROCESSING:
        analysis_data = get_analysis_status(study_material)
        if analysis_data["is_stale"] and study_material.analysis_retry_count >= MAX_RETRY_COUNT:
            raise RetryLimitExceededError(
                "재시도 횟수(최대 2회)를 모두 사용했습니다. 학습 작업을 직접 추가해주세요."
            )
        raise DuplicateAnalysisRequestError("이미 분석 중인 자료는 다시 분석할 수 없습니다.")

    if study_material.analysis_retry_count >= MAX_RETRY_COUNT:
        raise RetryLimitExceededError(
            "재시도 횟수(최대 2회)를 모두 사용했습니다. 학습 작업을 직접 추가해주세요."
        )

    raise DuplicateAnalysisRequestError(
        f"재시도할 수 없는 상태입니다 (현재 analysis_status: {status})."
    )


def run_claimed_analysis(study_material: StudyMaterial, run_id: uuid.UUID) -> list[StudyTask]:
    """
    claim_analysis_run()이 이미 PROCESSING으로 선점한(run_id 발급) material에
    대해 실제 AI 분석(외부 네트워크 호출 포함)을 수행한다. claim 단계와
    분리한 이유는 claim은 ExamPeriod 락 안에서, 이 함수는 락 밖에서 실행해야
    하기 때문이다 (claim → lock 해제 → 외부 작업 순서).
    """
    return _execute_analysis(study_material, run_id)


def analyze_and_estimate(study_material: StudyMaterial) -> list[StudyTask]:
    """
    E-AI-01 진입점 (claim + 실행을 한 번에). ExamPeriod 락 기준 선점이 필요
    없는 호출부(예: 유닛 테스트)를 위해 기존 시그니처를 그대로 유지한다.
    View에서 락 기준 선점이 필요하면 claim_analysis_run()/run_claimed_analysis()를
    따로 호출한다.
    """
    run_id = claim_analysis_run(study_material, is_retry=False)
    return run_claimed_analysis(study_material, run_id)


def retry_analysis(study_material: StudyMaterial) -> list[StudyTask]:
    """E-AI-03 진입점 (claim + 실행을 한 번에). 위와 동일한 이유로 유지."""
    run_id = claim_analysis_run(study_material, is_retry=True)
    return run_claimed_analysis(study_material, run_id)


def get_analysis_status(study_material: StudyMaterial) -> dict:
    """

    Returns:
        {
            "status": "pending" | "processing" | "completed" | "failed",
            "error_message": str | None,       # FAILED가 아니면 항상 None
            "retry_count": int,                # 지금까지 사용자가 재시도한 횟수
            "retry_remaining": int,             # 남은 재시도 가능 횟수 (0~2)
            "is_stale": bool,                  # PROCESSING인데 타임아웃을 넘겨 "좀비" 상태인지
                                                 # (analysis_started_at이 NULL인 경우도 좀비로 간주)
            "can_retry": bool,                 # 지금 이 순간 재시도 버튼을 활성화해도 되는지
            "retry_after_seconds": int | None, # PROCESSING이라 아직 재시도 못 하는 경우,
                                                 # 좀비 판정까지 남은 초 (그 외엔 None)
        }

    can_retry / retry_after_seconds를 추가한 이유: 프론트가 "5분 지났는지"를
    직접 타이머로 계산하게 하면, 서버 시각과 클라이언트 시각이 어긋나거나 화면을
    켜둔 채 방치했을 때 오차가 생길 수 있다. 그 대신 폴링할 때마다 서버가 판단한
    결과(can_retry)를 그대로 내려줘서, 프론트는 이 값만 보고 버튼을 켜고 끄면 된다.
    판정 기준은 retry_analysis()가 실제로 허용하는 조건과 동일하게 맞췄다:
        - 텍스트 추출(status)이 COMPLETED이고
        - 재시도 횟수가 남아있고(retry_remaining > 0)
        - analysis_status가 FAILED이거나, PROCESSING이면서 좀비 상태(is_stale)일 때
    """
    is_stale = False
    elapsed_seconds: float | None = None
    if study_material.analysis_status == MaterialStatus.PROCESSING:
        if study_material.analysis_started_at is None:
            is_stale = True
        else:
            elapsed_seconds = (
                timezone.now() - study_material.analysis_started_at
            ).total_seconds()
            is_stale = elapsed_seconds >= PROCESSING_TIMEOUT_SECONDS

    retry_remaining = max(0, MAX_RETRY_COUNT - study_material.analysis_retry_count)

    can_retry = False
    retry_after_seconds: int | None = None

    if study_material.status == MaterialStatus.COMPLETED and retry_remaining > 0:
        if study_material.analysis_status == MaterialStatus.FAILED:
            can_retry = True
        elif study_material.analysis_status == MaterialStatus.PROCESSING:
            if is_stale:
                can_retry = True
            elif elapsed_seconds is not None:
                retry_after_seconds = max(
                    0, int(PROCESSING_TIMEOUT_SECONDS - elapsed_seconds)
                )
            else:
                # 이 분기는 사실상 도달하지 않는다 (started_at이 None이면 위에서
                # 이미 is_stale=True로 처리됨). 방어적으로 타임아웃 전체를 남겨둔다.
                retry_after_seconds = PROCESSING_TIMEOUT_SECONDS
        # PENDING/COMPLETED(analysis_status)는 can_retry=False, retry_after_seconds=None 유지

    return {
        "status": study_material.analysis_status,
        "error_message": (
            study_material.analysis_error_message
            if study_material.analysis_status == MaterialStatus.FAILED
            else None
        ),
        "retry_count": study_material.analysis_retry_count,
        "retry_remaining": retry_remaining,
        "is_stale": is_stale,
        "can_retry": can_retry,
        "retry_after_seconds": retry_after_seconds,
    }