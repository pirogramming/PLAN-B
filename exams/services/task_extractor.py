"""
BE3 담당 - AI 분석: 시험 범위 텍스트 -> 단원 분리 -> 학습 작업 생성

담당 범위 (기획 문서 8장 기준):
- 추출된 시험 범위 텍스트를 AI 입력 형식으로 변환
- AI에게 단원 분리, 학습 작업 생성, task_type/importance/depth/difficulty 추천,
  추천 이유(ai_reason) 생성을 요청
- AI 응답 JSON 검증 및 실패 처리
- 생성되는 각 StudyTask는 20~60분 크기로 쪼개지도록 유도
  (BE1의 time_estimator가 "StudyTask 1개 = 20~60분" 전제로 계산하기 때문)

담당하지 않는 것 (규칙 엔진의 몫 = BE1):
- estimated_min_minutes / estimated_max_minutes 최종 계산
  -> 이 서비스는 두 값을 0으로 남겨두고, 이후 planner 쪽 time_estimator가 채운다.
- 가능성 판정, 시간 배분, 일정 생성, 복구안 생성

주의:
- 여기서 생성한 StudyTask는 항상 is_confirmed=False, is_user_modified=False 상태로 저장된다.
  사용자가 검토/수정/확정하기 전까지는 최종 계획 계산에 사용하지 않는다 (기획 원칙 #14, #15).

AI 제공사 (2026-08 기준):
- Google Gemini API(google-genai SDK) 사용. 팀 예산상 무료/저비용으로 진행하기 위해
  Anthropic 대신 Gemini로 전환했다 (기본 모델: gemini-3.1-flash-lite).
- google.genai.errors.APIError를 잡아 재시도 처리한다.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from django.conf import settings
from django.db import transaction

from core.choices import TaskType, PriorityLevel, TaskDepth, TaskDifficulty
from core.exceptions import AICallFailedError, AIResponseValidationError
from exams.models import Exam, StudyMaterial, StudyTask

logger = logging.getLogger(__name__)

# AI 호출(네트워크/API) 실패 시 재시도 횟수 (최초 시도 포함 총 MAX_RETRIES + 1회 시도)
MAX_RETRIES = 2

# AI 응답이 JSON 검증에 실패했을 때 self-correction 재요청 횟수
MAX_VALIDATION_RETRIES = 1

# 같은 입력에 대해 결과 편차(특히 작업 개수)를 줄이기 위한 고정 seed.
# 완전한 결정론을 보장하진 않지만(모델 특성상), temperature와 함께 재현성을 높인다.
GENERATION_SEED = 42

# Django model의 choices가 이 프로젝트에서 "허용되는 값"의 유일한 기준(source of
# truth)이다. _RESPONSE_SCHEMA(API에 강제하는 JSON 스키마)도 아래 값들을 그대로
# 재사용한다 - enum을 따로 하드코딩해두면, 나중에 core.choices의 선택지가 바뀔 때
# 스키마 쪽을 깜빡하고 안 고쳐서 "AI API가 허용하는 값"과 "모델이 실제로 허용하는
# 값"이 어긋나는 사고가 날 수 있다.
_VALID_TASK_TYPES = {c[0] for c in TaskType.choices}
_VALID_IMPORTANCE = {c[0] for c in PriorityLevel.choices}
_VALID_DEPTH = {c[0] for c in TaskDepth.choices}
_VALID_DIFFICULTY = {c[0] for c in TaskDifficulty.choices}

_REQUIRED_TASK_FIELDS = {
    "unit_name", "title", "task_type", "importance", "depth", "difficulty", "ai_reason",
}

# API 차원에서 JSON 스키마를 강제한다. response_mime_type="application/json"만으로는
# "JSON이라는 것"만 보장하고 필드 구조/enum 값까지는 강하게 제한하지 않는데, 이걸
# 추가하면 필드 누락·enum 오타 같은 형식 오류 자체가 줄어들어(Python 검증 단계에서
# 걸러내기 전에 API가 먼저 막아줌) self-correction 재요청 빈도가 줄어든다.
# 주의: 이건 "형식"이 맞다는 것만 보장하지, "의미"(예: 작업을 몇 개로 나눌지)까지
# 안정시켜주지는 않는다 - 그건 프롬프트의 명시적 규칙(4번)이 담당한다.
#
# enum/required 값은 위 _VALID_*, _REQUIRED_TASK_FIELDS를 그대로 재사용한다
# (직접 하드코딩하지 않음 - 단일 소스 오브 트루스 유지).
_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "unit_name": {"type": "string"},
                    "title": {"type": "string"},
                    "task_type": {
                        "type": "string",
                        "enum": sorted(_VALID_TASK_TYPES),
                    },
                    "importance": {
                        "type": "string",
                        "enum": sorted(_VALID_IMPORTANCE),
                    },
                    "depth": {
                        "type": "string",
                        "enum": sorted(_VALID_DEPTH),
                    },
                    "difficulty": {
                        "type": "string",
                        "enum": sorted(_VALID_DIFFICULTY),
                    },
                    "ai_reason": {"type": "string"},
                    "source_pages": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                },
                "required": sorted(_REQUIRED_TASK_FIELDS),
            },
        },
    },
    "required": ["tasks"],
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
    source_pages: list[int] = field(default_factory=list)


def build_prompt(exam_name: str, exam_date, source_text: str, previous_error: str | None = None) -> str:
    """
    시험 범위 원문 텍스트를 AI 프롬프트로 변환한다.

    previous_error가 주어지면, 직전 응답이 검증에 실패한 이유를 프롬프트에 덧붙여
    AI가 스스로 형식을 고쳐 재응답하도록 유도한다 (self-correction).
    """
    prompt = f"""너는 학생의 시험 범위를 학습 작업 단위로 쪼개는 도우미다.

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
4. 각 학습 작업은 일반적으로 20~60분 안에 완료할 수 있는 크기를 목표로 한다
   (이하 "크기 목표"). 다만 크기 목표 자체가 작업을 몇 개로 나눌지를 직접
   결정하지는 않는다 - 아래 b의 명시적 조건에 해당할 때만 나누고, 그 외에는
   크기 목표를 조금 벗어나더라도 임의로 쪼개지 마라. 하나의 단원 안에서
   작업을 몇 개 만들지는 아래 순서를 그대로 따라 정하라. 추상적으로 "적당히",
   "필요하면" 판단하지 말고, 이 순서대로 조건을 하나씩 확인하라 - 이게
   지켜지지 않으면 같은 자료를 다시 분석해도 매번 작업 개수가 달라진다.
   a. 기본값: 하나의 단원에는 작업을 1개만 만든다.
   b. 아래 조건 중 하나라도 원문에 명시적으로 나타나면, 그 조건에 맞춰서만
      작업을 분리한다.
      - 개념 설명(장점/구조/정의/비교)과 실제 구현 절차(삽입/삭제/순회 같은
        동작을 코드로 보여주는 부분)가 원문에 둘 다 있다
        → concept 작업과 practice 작업으로 분리한다. 특정 단원이라고 이
          구분을 생략하고 전부 concept 하나로 뭉치지 마라.
      - 서로 다른 이름이 붙은 예제나 응용 사례가 2개 이상 있다
        (예: "다항식 표현 예제", "학생 정보 관리 예제", "은행 시뮬레이션 예제"처럼
        각각 독립된 이름과 목적을 가진 예제들)
        → 예제별로 별도 작업으로 분리하거나, 하나로 묶을 거면 title과
          ai_reason에 포함된 예제를 전부 언급한다. 일부만 언급하고 나머지는
          source_pages에만 슬쩍 끼워넣지 마라.
      - 개념 이해 / 비교·정리 / 계산·적용 / 문제 풀이 / 최종 복습처럼 서로
        다른 학습 행동이 원문에 각각 명시되어 있다
        → 학습 행동별로 분리한다.
   c. 위 b의 조건에 하나도 해당하지 않으면 절대 임의로 나누지 마라.
      "내용이 많아 보여서", "크기 목표보다 클 것 같아서" 같은 막연한 느낌만으로
      작업을 쪼개지 마라. 분리 여부는 오직 b에서 나열한 명시적 조건으로만 정한다.
   d. 반대로 어떤 단원이 원문에서 지나치게 넓은 범위를 가리켜서(예: 여러
      장에 걸친 원문 전체) b를 적용해도 각 작업이 크기 목표보다 훨씬
      커진다면, 작업만 여러 개로 쪼개지 말고 그 단원 자체를 원문의 소제목
      기준으로 더 잘게 나눈 뒤, 그 안에서 a~c를 다시 적용하라.
   e. 제목만 다르고 실질적으로 같은 내용을 다루는 작업을 중복 생성하지 마라.
      (예: "OSI 계층 구조 학습"과 "OSI 계층별 역할 비교"는 다루는 내용이
      실제로 다르면 둘 다 유지해도 되지만, 사실상 같은 내용을 제목만 바꿔
      두 번 만드는 것은 안 된다.)
   f. 학습 행동이 다르면 그에 맞는 task_type을 지정하되, task_type을 다르게
      보이게 하려고 내용과 맞지 않는 유형을 억지로 지정하지는 마라 (예: 문제
      풀이가 없는데 practice로 표시하지 않는다).
5. 사용자가 제공한 시험범위에 명시되지 않은 개념, 공식, 예제 또는 세부 주제를
   새롭게 추가하지 마라. 선수 학습이 필요하다고 판단되더라도, 입력 내용 안에
   실제로 언급된 것만으로 작업을 생성하라. (예: 입력에 "푸리에 급수의 개념"만
   있으면 "직교성" 같은 세부 개념을 임의로 덧붙이지 않는다.)
6. importance / depth / difficulty는 아래 기준표를 그대로 적용해서 정하라.
   추측하지 말고, 원문에 실제로 나타난 신호를 기준으로 판단하라.

   importance (중요도):
   - high: 원문에서 "핵심", "중요", "필수"라고 명시되었거나, 다른 단원/개념의
     선수 지식으로 명시적으로 언급된 경우
   - medium: 위 조건에 해당하지 않는 일반적인 시험 범위 내용 (기본값)
   - low: 원문에서 "참고", "부가", "선택"이라고 명시된 경우

   depth (깊이):
   - core: 다른 범위를 이해하는 데 반드시 필요한 선수 핵심 개념
   - basic: 독립적으로 이해 가능한 일반 시험 범위 내용 (기본값)
   - optional: 원문에서 참고/부가/선택으로 명시된 내용

   difficulty (난이도):
   - easy: 정의, 단순 암기, 기본적인 사실 확인 수준
   - normal: 개념 이해, 비교, 일반적인 적용 수준 (기본값)
   - hard: 여러 개념을 결합해야 하거나, 복잡한 계산·구현·문제 풀이가 필요한 수준

   원문에 명시적 신호가 없어서 확신하기 어려운 항목은 각 기준의 기본값
   (medium / basic / normal)을 사용하라. 모든 작업을 high/core/hard로
   분류하지 마라 - 상대적으로 덜 중요하거나 쉬운 내용도 있다면 그에 맞게 구분하라.
7. ai_reason을 작성할 때, 사용자가 제공한 내용에 출제 빈도·교수자 강조·배점
   정보가 없다면 "시험에 자주 출제된다", "출제 비중이 높다", "배점이 높다"
   같은 근거 없는 단정적 표현을 쓰지 마라. 중요도 추천 이유는 선수 관계,
   계산 복잡도, 다른 개념과의 연결성 등 입력 내용 자체에서 추론 가능한
   근거로만 작성하라.
8. 입력 텍스트 안에 "--- 페이지 N ---" 형식의 표시가 있다면, 이는 PDF에서
   그 표시 다음 내용이 실제로 몇 번째 페이지에 있었는지 알려주는 것이다. 각
   작업을 만들 때, 그 작업의 근거가 된 내용이 실제로 있었던 모든 페이지
   번호를 source_pages 필드에 정수 배열로 표시하라.
   - 한 작업의 내용이 여러 페이지에 걸쳐 있다면, 관련된 페이지를 전부
     나열하라 (연속되지 않은 페이지가 섞여 있어도 된다. 예: [5, 6, 9]).
   - 대표 페이지 하나만 고르지 말고, 그 작업과 실제로 관련된 페이지를
     빠짐없이 담아라.
   - 입력 텍스트에 "--- 페이지 N ---" 표시가 전혀 없다면(텍스트를 직접 입력한
     경우 등), source_pages는 빈 배열 []로 응답하라. 페이지 정보가 없는데
     임의로 숫자를 지어내지 마라.
   - 어떤 페이지의 내용이 다른 단원에서 이미 다룬 것과 사실상 같아서
     그 페이지만을 위한 새 작업을 만들지 않기로 했다면(규칙 4 참고),
     그 페이지를 빠뜨리지 말고 그 중복 대상이 된 기존 작업의
     source_pages에 포함시켜라. (예: 헤더 노드 방식의 삽입/삭제 코드가
     이미 다른 단원에서 다룬 포인터 조작과 같은 기법이라 새 작업을
     안 만들기로 했다면, 그 페이지 번호는 그 기존 작업의 source_pages에
     추가한다. 새 작업을 안 만든다고 해서 그 페이지를 통째로 누락시키지 마라.)
   - 하나의 작업 안에서 다루는 내용이 연속된 여러 페이지에 걸쳐 있다면,
     그 범위 중간에 있는 페이지를 빠뜨리지 마라. (예: 어떤 작업의 근거가
     14, 15, 16, 17, 18, 19, 20페이지에 걸쳐 쭉 이어지는데, 그중 17, 18만
     실수로 누락해서 [14, 15, 16, 19, 20]으로 응답하지 않는다.) 각 페이지의
     실제 내용을 다시 확인해서, 그 작업과 같은 주제로 이어지는 페이지는
     빠짐없이 포함시켰는지 점검하라.
   - 단, 페이지를 빠뜨리지 않으려고 서로 다른 학습 목표를 가진 내용을
     하나의 작업으로 억지로 합치지 마라. 규칙 4가 여전히 우선한다.
     예를 들어 "핵심 알고리즘 개념/구현"과 "그 알고리즘을 활용한 별도
     응용 예제"(다항식 표현, 학생 정보 관리, 음악 재생목록, 이미지 뷰어 등)는
     원래 기준대로 서로 다른 작업으로 나누고, 그 대신 나눈 각 작업의
     source_pages에 그 작업이 실제로 다루는 페이지만 정확히 담아라.
     "여러 페이지를 한 작업으로 합쳐서 구멍을 없애는 것"이 아니라, "제대로
     나눈 뒤 각 작업의 페이지 범위를 정확하고 빠짐없이 담는 것"이 목표다.
9. 반드시 아래 JSON 형식으로만 응답하라. 다른 설명, 마크다운 코드블록, 접두사를 붙이지 마라.

{{
  "tasks": [
    {{
      "unit_name": "string",
      "title": "string",
      "task_type": "concept | practice | review | summary | custom",
      "importance": "high | medium | low",
      "depth": "core | basic | optional",
      "difficulty": "easy | normal | hard",
      "ai_reason": "string",
      "source_pages": "int 배열 (예: [5, 6]). 페이지 표시가 입력에 없으면 빈 배열 []"
    }}
  ]
}}
"""
    if previous_error:
        prompt += f"""

[이전 응답 거부됨] 방금 전 응답이 다음 이유로 거부되었다:
{previous_error}

위 규칙과 JSON 형식을 다시 한번 정확히 지켜서, 순수 JSON만 응답하라."""
    return prompt


_MOCK_RESPONSE = """{
  "tasks": [
    {
      "unit_name": "1장 신호의 기본 개념",
      "title": "1장 핵심 개념 읽기",
      "task_type": "concept",
      "importance": "high",
      "depth": "core",
      "difficulty": "normal",
      "ai_reason": "1장은 이후 단원의 기초 개념이라 우선 이해가 필요합니다.",
      "source_pages": [4, 5]
    },
    {
      "unit_name": "1장 신호의 기본 개념",
      "title": "1장 예제 풀이",
      "task_type": "practice",
      "importance": "medium",
      "depth": "basic",
      "difficulty": "easy",
      "ai_reason": "기본 개념 확인용 예제로 난이도가 낮습니다.",
      "source_pages": []
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

    client = genai.Client(api_key=settings.GOOGLE_API_KEY)
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 2):
        try:
            response = client.models.generate_content(
                model=settings.AI_MODEL_NAME,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    # 분류/구조화 작업이라 창의성보다 일관성이 중요함 -> 낮은 온도로 설정
                    # (기본값 ~1.0은 매번 답이 크게 달라질 수 있어 분류 작업엔 부적합)
                    temperature=0.2,
                    # 같은 입력에 대한 결과 편차(작업 개수 등)를 줄이기 위한 고정 seed.
                    # temperature만으로는 편차를 완전히 못 줄여서 같이 사용한다 - 다만
                    # 이것만으로 완전한 결정론이 보장되진 않는다 (모델 자체의 특성).
                    seed=GENERATION_SEED,
                    # API 차원에서 JSON 형식을 강제 -> _strip_code_fence()로도 못 거르는
                    # 이상 응답(코드블록 등) 자체를 줄여줌
                    response_mime_type="application/json",
                    # 필드 구조/enum 값까지 API 차원에서 강제 -> 형식 오류로 인한
                    # self-correction 재요청 빈도를 줄임 (의미적 일관성까지 보장하진
                    # 않음 - 그건 프롬프트의 명시적 규칙이 담당)
                    response_schema=_RESPONSE_SCHEMA,
                ),
            )
            return response.text
        except genai_errors.APIError as exc:
            last_error = exc
            logger.warning("AI 호출 실패 (시도 %d/%d): %s", attempt, MAX_RETRIES + 1, exc)

    raise AICallFailedError(f"AI 호출이 {MAX_RETRIES + 1}회 모두 실패했습니다: {last_error or '알 수 없는 에러'}")


def _strip_code_fence(raw: str) -> str:
    """AI가 실수로 마크다운 코드블록을 붙여 응답한 경우를 대비해 제거한다."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
    return text.strip()


def _parse_and_validate(raw_response: str, valid_pages: set[int] | None = None) -> list[ExtractedTask]:
    """
    AI 응답 텍스트를 검증하고 ExtractedTask 목록으로 변환한다.

    valid_pages: 입력 텍스트에 실제로 존재하는 페이지 번호 집합(_extract_available_page_numbers()
    참고). 주어지면 각 작업의 source_pages를 이 집합 기준으로 한 번 더 걸러낸다 - AI가 응답
    스키마(정수 배열이라는 "형식")는 지켰지만 실제로 존재하지 않는 페이지 번호를 지어낸
    경우(예: 문서가 20페이지인데 21을 반환)를 막기 위함이다. None이면 이 검증을 건너뛴다.
    """
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
            source_pages=_parse_source_pages(item.get("source_pages"), valid_pages),
        ))

    return tasks


_PAGE_MARKER_PATTERN = re.compile(r"--- 페이지 (\d+) ---")


def _extract_available_page_numbers(text: str) -> set[int]:
    """
    입력 텍스트 안에 실제로 존재하는 "--- 페이지 N ---" 마커의 페이지 번호를 전부
    모아 집합으로 반환한다. pdf_extractor.py가 텍스트가 없는 페이지는 마커 자체를
    안 남기므로(연속되지 않은 번호가 있을 수 있음), 이 집합이 "1부터 마지막 페이지까지의
    범위"가 아니라 "실제로 텍스트가 있었던 페이지들의 정확한 목록"이라는 점에 유의한다.
    마커가 하나도 없으면(텍스트 직접 입력 등) 빈 집합을 반환한다.
    """
    return {int(n) for n in _PAGE_MARKER_PATTERN.findall(text)}


def _parse_source_pages(raw_value, valid_pages: set[int] | None = None) -> list[int]:
    """
    source_pages는 필수 필드가 아니다 (텍스트 직접 입력 등 페이지 개념이 없는
    입력에서는 항상 빈 배열이다). 다른 필드와 달리, 이 값이 이상하다고 해서
    전체 응답을 거부하고 재요청하게 만들 정도의 가치는 없다고 판단해서, 개별
    원소가 이상해도 전체를 실패시키지 않고 유효한 값만 골라서 담는다
    (부가 정보 하나 때문에 self-correction 재시도를 태우는 건 낭비).

    - 정수/숫자 문자열만 인정, 0 이하나 bool, 그 외 형식은 조용히 버림
    - 중복 제거, 오름차순 정렬
    - raw_value 자체가 리스트가 아니면(응답 형식이 완전히 틀어진 경우) 빈 리스트
    - valid_pages가 주어지면(입력 텍스트에 실제로 존재하는 페이지 번호 집합),
      그 안에 없는 페이지 번호는 걸러낸다. response_schema는 "정수 배열"이라는
      형식만 강제하지, 그 정수가 실제 문서 범위 안의 페이지인지는 보장하지
      않는다 - 예를 들어 문서가 20페이지인데 AI가 21을 반환해도 스키마는
      통과하므로, 여기서 실제 페이지 목록과 대조해 한 번 더 걸러낸다.
      None이면(문맥상 실제 페이지 목록을 알 수 없는 경우) 이 단계를 건너뛴다.
    """
    if not isinstance(raw_value, list):
        return []

    pages: set[int] = set()
    for item in raw_value:
        if isinstance(item, bool):  # bool은 int의 서브클래스라 명시적으로 제외
            continue
        if isinstance(item, int) and item > 0:
            pages.add(item)
        elif isinstance(item, str) and item.strip().isdigit():
            pages.add(int(item.strip()))

    if valid_pages is not None:
        invalid = pages - valid_pages
        if invalid:
            logger.warning(
                "AI가 실제 문서에 존재하지 않는 페이지 번호를 반환해 제거함: %s",
                sorted(invalid),
            )
        pages &= valid_pages

    return sorted(pages)


def fetch_extracted_tasks(exam: Exam, extracted_text: str) -> list[ExtractedTask]:
    """
    AI 호출 + 파싱 + 검증만 수행한다. DB 접근이 전혀 없고, 트랜잭션도 걸지 않는다.

    (기존 이슈: analyze_study_material() 전체가 @transaction.atomic이라, 그 안에서
    벌어지는 AI 네트워크 호출(재시도 포함 최대 수십 초 소요 가능)이 DB 커넥션을
    오래 점유하는 문제가 있었다. 이 함수는 순수 네트워크/파싱만 담당해서 DB 트랜잭션과
    완전히 분리한다 - 호출하는 쪽에서 결과를 받은 뒤 별도로, 가능한 한 짧게 DB에 저장해야 한다.)

    AI 응답이 JSON 검증에 실패하면, 실패 이유를 프롬프트에 덧붙여 최대
    MAX_VALIDATION_RETRIES회 self-correction 재요청을 한다.

    실패 시 AIAnalysisError 계열 예외(AICallFailedError, AIResponseValidationError)를
    발생시킨다.
    """
    # 입력 텍스트에 실제로 존재하는 페이지 번호를 미리 추출해둔다. AI가 source_pages에
    # 이 목록에 없는 페이지(예: 문서에 없는 21페이지)를 지어내 응답하더라도,
    # _parse_and_validate()에서 이 목록 기준으로 걸러낸다 (리뷰 반영).
    valid_pages = _extract_available_page_numbers(extracted_text)

    extracted_tasks = None
    last_validation_error: str | None = None

    for attempt in range(MAX_VALIDATION_RETRIES + 1):
        prompt = build_prompt(
            exam.subject_name,
            exam.exam_date,
            extracted_text,
            previous_error=last_validation_error,
        )
        raw_response = _call_ai(prompt)
        try:
            extracted_tasks = _parse_and_validate(raw_response, valid_pages)
            break
        except AIResponseValidationError as exc:
            last_validation_error = str(exc)
            logger.warning(
                "AI 응답 검증 실패 (재시도 %d/%d): %s",
                attempt + 1, MAX_VALIDATION_RETRIES + 1, exc,
            )

    if extracted_tasks is None:
        raise AIResponseValidationError(
            f"AI 응답 검증이 {MAX_VALIDATION_RETRIES + 1}회 모두 실패했습니다: {last_validation_error}"
        )

    return extracted_tasks


@transaction.atomic
def save_extracted_tasks(
    study_material: StudyMaterial, extracted_tasks: list[ExtractedTask]
) -> list[StudyTask]:
    """
    fetch_extracted_tasks()가 만든 결과를 StudyTask로 저장한다. DB 쓰기 전용이라
    네트워크 호출 없이 짧게 끝나므로, 트랜잭션으로 묶어도 DB 커넥션을 오래 점유하지 않는다.

    - 생성된 StudyTask는 is_confirmed=False, is_user_modified=False 상태로 저장된다.
      사용자가 검토/수정/확정하기 전까지는 최종 계획에 사용되지 않는다.
    - 동일 StudyMaterial에 대해 다시 실행되면(재분석), 이전에 생성된 미확정·미수정
      StudyTask는 삭제하고 새로 만든다 (사용자가 수정했거나 확정한 작업은 보존).
    """
    exam: Exam = study_material.exam

    # 동일 StudyMaterial로 "AI 분석 다시 실행"을 하는 경우, 이전에 생성된 미확정/미수정
    # StudyTask가 계속 누적되는 것을 방지하기 위해 먼저 정리한다.
    # (사용자가 직접 수정했거나(is_user_modified) 확정한(is_confirmed) 작업은 건드리지 않는다)
    StudyTask.objects.filter(
        study_material=study_material,
        is_confirmed=False,
        is_user_modified=False,
    ).delete()

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
            source_pages=task.source_pages,
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
    logger.info("AI 분석 완료: exam=%s, 생성된 작업 %d개", exam.subject_name, len(created_tasks))
    return created_tasks


def analyze_study_material(study_material: StudyMaterial) -> list[StudyTask]:
    """
    StudyMaterial의 추출된 텍스트를 AI로 분석해 StudyTask들을 생성한다.
    (fetch_extracted_tasks + save_extracted_tasks를 순서대로 호출하는 편의 함수)

    - AI가 생성하는 값: unit_name, title, task_type, importance, depth, difficulty, ai_reason
    - AI가 생성하지 않는 값: estimated_min/max_minutes
      -> 0으로 남겨두고, 이후 BE1의 time_estimator 서비스가 채운다.

    주의: 이 함수 자체는 트랜잭션으로 감싸져 있지 않다 (AI 네트워크 호출을 트랜잭션
    밖에 두기 위함). DB 쓰기는 save_extracted_tasks() 안에서만 짧게 트랜잭션 처리된다.
    이 함수를 다른 DB 작업과 원자적으로(atomic) 묶어야 하는 경우(예: 예상시간 계산까지
    한 번에 롤백되어야 하는 경우)에는, 이 함수 대신 fetch_extracted_tasks()로 AI 결과를
    먼저 받아온 뒤, 필요한 DB 작업들을 직접 하나의 @transaction.atomic으로 묶을 것
    (analysis_orchestrator.py의 _run_analysis_and_estimate() 참고).

    실패 시 AIAnalysisError 계열 예외(AICallFailedError, AIResponseValidationError)를
    발생시키며, 이 경우 StudyTask는 생성/삭제되지 않는다.
    """
    if not study_material.extracted_text:
        raise AIResponseValidationError("StudyMaterial에 분석할 텍스트가 없습니다.")

    exam: Exam = study_material.exam
    extracted_tasks = fetch_extracted_tasks(exam, study_material.extracted_text)
    return save_extracted_tasks(study_material, extracted_tasks)