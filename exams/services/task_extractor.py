"""
BE3 담당 - AI 분석: 시험 범위 텍스트 -> 단원 분리 -> 학습 작업 생성

담당 범위 (기획 문서 8장 기준):
- 추출된 시험 범위 텍스트를 AI 입력 형식으로 변환
- AI에게 단원 분리, 학습 작업 생성, task_type/importance/depth/difficulty 추천,
  추천 이유(ai_reason) 생성을 요청
- AI 응답 JSON 검증 및 실패 처리

담당하지 않는 것 (규칙 엔진의 몫 = BE1):
- estimated_min_minutes / estimated_max_minutes 최종 계산
  -> 이 서비스는 두 값을 0으로 남겨두고, 이후 planner 쪽 time_estimator가 채운다.
- 가능성 판정, 시간 배분, 일정 생성, 복구안 생성

주의:
- 여기서 생성한 StudyTask는 항상 is_confirmed=False, is_user_modified=False 상태로 저장된다.
  사용자가 검토/수정/확정하기 전까지는 최종 계획 계산에 사용하지 않는다 (기획 원칙 #14, #15).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from anthropic import Anthropic, APIError, APIConnectionError, APITimeoutError
from django.conf import settings
from django.db import transaction

from core.choices import TaskType, PriorityLevel, TaskDepth, TaskDifficulty
from core.exceptions import AICallFailedError, AIResponseValidationError
from exams.models import Exam, StudyMaterial, StudyTask

logger = logging.getLogger(__name__)

# AI 호출 실패 시 재시도 횟수 (최초 시도 포함 총 MAX_RETRIES + 1회 시도)
MAX_RETRIES = 2

_VALID_TASK_TYPES = {c[0] for c in TaskType.choices}
_VALID_IMPORTANCE = {c[0] for c in PriorityLevel.choices}
_VALID_DEPTH = {c[0] for c in TaskDepth.choices}
_VALID_DIFFICULTY = {c[0] for c in TaskDifficulty.choices}

_REQUIRED_TASK_FIELDS = {
    "unit_name", "title", "task_type", "importance", "depth", "difficulty", "ai_reason",
}


@dataclass
class ExtractedTask:
    """AI가 생성한 학습 작업 1건 (StudyTask 저장 직전 형태)"""
    unit_name: str
    title: str
    task_type: str
    importance: str
    depth: str
    difficulty: str
    ai_reason: str


def build_prompt(exam_name: str, exam_date, source_text: str) -> str:
    """시험 범위 원문 텍스트를 AI 프롬프트로 변환한다."""
    return f"""너는 학생의 시험 범위를 학습 작업 단위로 쪼개는 도우미다.

과목명: {exam_name}
시험일: {exam_date}

아래는 이 과목의 시험 범위 원문이다.

---
{source_text}
---

이 시험 범위를 학습 작업(StudyTask) 목록으로 분해하라.

규칙:
1. 먼저 단원(unit_name)을 나누고, 각 단원 안에서 학습 작업을 1개 이상 생성한다.
2. 각 작업은 다음 필드를 모두 가진다.
   - unit_name: 단원명 (예: "1장 신호의 기본 개념")
   - title: 작업명 (예: "1장 핵심 개념 읽기")
   - task_type: 다음 중 하나 - concept, practice, review, summary, custom
   - importance: 다음 중 하나 - high, medium, low
   - depth: 다음 중 하나 - core, basic, optional
   - difficulty: 다음 중 하나 - easy, normal, hard
   - ai_reason: 이 작업을 이렇게 분류한 이유를 1~2문장으로 설명
3. 예상 학습 시간(분)은 계산하지 마라. 이 단계에서는 다루지 않는다.
4. 반드시 아래 JSON 형식으로만 응답하라. 다른 설명, 마크다운 코드블록, 접두사를 붙이지 마라.

{{
  "tasks": [
    {{
      "unit_name": "string",
      "title": "string",
      "task_type": "concept | practice | review | summary | custom",
      "importance": "high | medium | low",
      "depth": "core | basic | optional",
      "difficulty": "easy | normal | hard",
      "ai_reason": "string"
    }}
  ]
}}
"""


_MOCK_RESPONSE = """{
  "tasks": [
    {
      "unit_name": "1장 신호의 기본 개념",
      "title": "1장 핵심 개념 읽기",
      "task_type": "concept",
      "importance": "high",
      "depth": "core",
      "difficulty": "normal",
      "ai_reason": "1장은 이후 단원의 기초 개념이라 우선 이해가 필요합니다."
    },
    {
      "unit_name": "1장 신호의 기본 개념",
      "title": "1장 예제 풀이",
      "task_type": "practice",
      "importance": "medium",
      "depth": "basic",
      "difficulty": "easy",
      "ai_reason": "기본 개념 확인용 예제로 난이도가 낮습니다."
    }
  ]
}"""


def _call_ai(prompt: str) -> str:
    """
    AI API를 호출한다. 실패 시 MAX_RETRIES회 재시도 후 예외를 던진다.

    settings.AI_MOCK_MODE가 True면 실제 API를 호출하지 않고 고정된 샘플 응답을 반환한다.
    (개발 중 크레딧/비용 소모 없이 파싱·검증·저장 로직을 테스트하기 위함)
    """
    if getattr(settings, "AI_MOCK_MODE", False):
        logger.info("AI_MOCK_MODE 활성화 상태 - 실제 API 호출 없이 샘플 응답 사용")
        return _MOCK_RESPONSE

    client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 2):
        try:
            response = client.messages.create(
                model=settings.AI_MODEL_NAME,
                max_tokens=4000,
                messages=[{"role": "user", "content": prompt}],
            )
            return "".join(
                block.text for block in response.content if block.type == "text"
            )
        except (APIError, APIConnectionError, APITimeoutError) as exc:
            last_error = exc
            logger.warning("AI 호출 실패 (시도 %d/%d): %s", attempt, MAX_RETRIES + 1, exc)

    raise AICallFailedError(f"AI 호출이 {MAX_RETRIES + 1}회 모두 실패했습니다: {last_error}")


def _strip_code_fence(raw: str) -> str:
    """AI가 실수로 마크다운 코드블록을 붙여 응답한 경우를 대비해 제거한다."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
    return text.strip()


def _parse_and_validate(raw_response: str) -> list[ExtractedTask]:
    """AI 응답 텍스트를 검증하고 ExtractedTask 목록으로 변환한다."""
    cleaned = _strip_code_fence(raw_response)

    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise AIResponseValidationError(f"AI 응답이 올바른 JSON이 아닙니다: {exc}") from exc

    if not isinstance(payload, dict) or "tasks" not in payload:
        raise AIResponseValidationError("AI 응답에 'tasks' 키가 없습니다.")

    raw_tasks = payload["tasks"]
    if not isinstance(raw_tasks, list) or len(raw_tasks) == 0:
        raise AIResponseValidationError("AI 응답의 'tasks'가 비어있거나 리스트가 아닙니다.")

    tasks: list[ExtractedTask] = []
    for i, item in enumerate(raw_tasks):
        if not isinstance(item, dict):
            raise AIResponseValidationError(f"{i}번째 작업이 객체(dict) 형태가 아닙니다.")

        missing = _REQUIRED_TASK_FIELDS - item.keys()
        if missing:
            raise AIResponseValidationError(f"{i}번째 작업에 누락된 필드: {missing}")

        if item["task_type"] not in _VALID_TASK_TYPES:
            raise AIResponseValidationError(
                f"{i}번째 작업의 task_type 값이 올바르지 않습니다: {item['task_type']}"
            )
        if item["importance"] not in _VALID_IMPORTANCE:
            raise AIResponseValidationError(
                f"{i}번째 작업의 importance 값이 올바르지 않습니다: {item['importance']}"
            )
        if item["depth"] not in _VALID_DEPTH:
            raise AIResponseValidationError(
                f"{i}번째 작업의 depth 값이 올바르지 않습니다: {item['depth']}"
            )
        if item["difficulty"] not in _VALID_DIFFICULTY:
            raise AIResponseValidationError(
                f"{i}번째 작업의 difficulty 값이 올바르지 않습니다: {item['difficulty']}"
            )

        tasks.append(ExtractedTask(
            unit_name=str(item["unit_name"]).strip(),
            title=str(item["title"]).strip(),
            task_type=item["task_type"],
            importance=item["importance"],
            depth=item["depth"],
            difficulty=item["difficulty"],
            ai_reason=str(item["ai_reason"]).strip(),
        ))

    return tasks


@transaction.atomic
def analyze_study_material(study_material: StudyMaterial) -> list[StudyTask]:
    """
    StudyMaterial의 추출된 텍스트를 AI로 분석해 StudyTask들을 생성한다.

    - AI가 생성하는 값: unit_name, title, task_type, importance, depth, difficulty, ai_reason
    - AI가 생성하지 않는 값: estimated_min/max_minutes
      -> 0으로 남겨두고, 이후 BE1의 time_estimator 서비스가 채운다.
    - 생성된 StudyTask는 is_confirmed=False, is_user_modified=False 상태로 저장된다.
      사용자가 검토/수정/확정하기 전까지는 최종 계획에 사용되지 않는다.

    실패 시 AIAnalysisError 계열 예외(AICallFailedError, AIResponseValidationError)를
    발생시키며, 이 경우 StudyTask는 생성되지 않는다 (트랜잭션 롤백).
    """
    if not study_material.extracted_text:
        raise AIResponseValidationError("StudyMaterial에 분석할 텍스트가 없습니다.")

    exam: Exam = study_material.exam
    prompt = build_prompt(exam.name, exam.exam_date, study_material.extracted_text)

    raw_response = _call_ai(prompt)
    extracted_tasks = _parse_and_validate(raw_response)

    existing_max_order = (
        StudyTask.objects
        .filter(exam=exam)
        .order_by("-order")
        .values_list("order", flat=True)
        .first() or 0
    )

    created_tasks = [
        StudyTask(
            exam=exam,
            study_material=study_material,
            unit_name=task.unit_name,
            title=task.title,
            task_type=task.task_type,
            importance=task.importance,
            depth=task.depth,
            difficulty=task.difficulty,
            ai_reason=task.ai_reason,
            estimated_min_minutes=0,
            estimated_max_minutes=0,
            is_user_modified=False,
            is_confirmed=False,
            order=existing_max_order + i,
        )
        for i, task in enumerate(extracted_tasks, start=1)
    ]

    # 주의: bulk_create는 StudyTask.save()의 full_clean()을 거치지 않는다.
    # 여기서 만드는 값들은 이미 choices 검증을 마쳤고 min/max가 둘 다 0이라 문제없지만,
    # 추후 필드가 늘어나면 이 부분을 다시 확인할 것.
    StudyTask.objects.bulk_create(created_tasks)
    logger.info("AI 분석 완료: exam=%s, 생성된 작업 %d개", exam.name, len(created_tasks))
    return created_tasks