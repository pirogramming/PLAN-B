"""
실제 API(Gemini)로 서로 다른 과목 3개 테스트하는 스크립트.
특히 importance/depth/difficulty 판단이 과목/내용에 따라 잘 구분되는지 확인용.

사용법:
    1. https://aistudio.google.com 에서 무료 API 키 발급
    2. .env에서 AI_MOCK_MODE=False 로 바꾸고, GOOGLE_API_KEY에 발급받은 키 넣기
    3. 프로젝트 루트(manage.py 있는 폴더)에서 실행:
       python manage.py shell < test_real_api_3subjects.py

주의: 실제 API를 3번 호출합니다. Google AI Studio 무료 티어는 분당/일별 호출 횟수
제한이 있으니, 너무 자주 반복 실행하지 마세요.
"""
import django, os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from datetime import date
from collections import Counter
from django.conf import settings
from accounts.models import User
from exams.models import ExamPeriod, Exam, StudyMaterial
from exams.services.analysis_orchestrator import analyze_and_estimate, get_analysis_status

if settings.AI_MOCK_MODE:
    print("!! AI_MOCK_MODE가 True입니다. .env에서 AI_MOCK_MODE=False로 바꾸고 다시 실행하세요.")
    raise SystemExit(1)

if not settings.GOOGLE_API_KEY:
    print("!! GOOGLE_API_KEY가 비어있습니다. .env에 발급받은 키를 넣어주세요.")
    raise SystemExit(1)

SAMPLES = [
    {
        "subject_name": "신호및시스템",
        "exam_date": date(2026, 8, 17),
        "text": (
            "1장 신호의 기본 개념: 연속시간 신호와 이산시간 신호의 정의, 주기 신호와 "
            "비주기 신호의 구분, 짝함수와 홀함수. "
            "2장 시스템의 특성: 선형성, 시불변성, 인과성, 안정성의 정의와 판별법. "
            "3장 푸리에 변환: 푸리에 급수의 개념, 연속시간 푸리에 변환의 정의와 성질, "
            "대표적인 신호들의 푸리에 변환 쌍."
        ),
    },
    {
        "subject_name": "데이터통신",
        "exam_date": date(2026, 8, 19),
        "text": (
            "2장 네트워크 프로토콜 개요: OSI 7계층과 TCP/IP 4계층 모델 비교, 각 계층의 역할. "
            "3장 TCP/IP: TCP의 3-way handshake 과정, TCP와 UDP의 차이, IP 주소 체계와 서브넷 마스크."
        ),
    },
    {
        "subject_name": "공학수학",
        "exam_date": date(2026, 8, 21),
        "text": (
            "4장 복소수: 복소수의 극형식과 오일러 공식, 복소평면에서의 연산. "
            "5장 미분방정식: 1계 선형 미분방정식의 풀이법, 2계 상수계수 동차 미분방정식, "
            "라플라스 변환을 이용한 미분방정식 풀이."
        ),
    },
]

user, _ = User.objects.get_or_create(username="quality_test", email="quality_test@example.com")
period, _ = ExamPeriod.objects.get_or_create(
    user=user, title="실제 API 품질 테스트",
    defaults={"start_date": date(2026, 8, 1), "end_date": date(2026, 8, 25)}
)

all_tasks = []

for sample in SAMPLES:
    print(f"\n{'='*60}")
    print(f"과목: {sample['subject_name']}")
    print('='*60)

    exam, _ = Exam.objects.get_or_create(
        exam_period=period, subject_name=sample["subject_name"],
        defaults={"exam_date": sample["exam_date"]}
    )
    material = StudyMaterial.objects.create(
        exam=exam, title=f"{sample['subject_name']} 테스트 범위",
        extracted_text=sample["text"],
    )

    try:
        tasks = analyze_and_estimate(material)
    except Exception as e:
        print(f"!! 분석 실패: {type(e).__name__}: {e}")
        print("상태:", get_analysis_status(material))
        continue

    all_tasks.extend(tasks)
    print(f"생성된 작업 개수: {len(tasks)}\n")
    for t in tasks:
        print(f"- [{t.unit_name}] {t.title}")
        print(f"    유형/중요도/깊이/난이도: {t.task_type}/{t.importance}/{t.depth}/{t.difficulty}")
        print(f"    예상시간: {t.estimated_min_minutes}~{t.estimated_max_minutes}분")
        print(f"    추천 이유: {t.ai_reason}")
        print()

print(f"\n{'='*60}")
print("전체 분포 요약 (여기가 한쪽으로 몰려있으면 판단을 잘 못하고 있다는 신호)")
print('='*60)
print("중요도(importance):", dict(Counter(t.importance for t in all_tasks)))
print("깊이(depth):        ", dict(Counter(t.depth for t in all_tasks)))
print("난이도(difficulty):  ", dict(Counter(t.difficulty for t in all_tasks)))
print("작업유형(task_type): ", dict(Counter(t.task_type for t in all_tasks)))

print("\n확인 포인트:")
print("- 작업이 지나치게 넓은 학습 범위를 한 항목에 묶지 않았는지")
print("- 하나의 작업이 독립적으로 수행 가능한 학습 단위인지")
print("- task_type 기준 기본시간이 정책 범위에 맞는지")
print("  (참고: 20~60분은 task_type별 '기본' 작업 단위 기준이고,")
print("   난이도 배율·speed_factor가 적용된 최종 estimated_min/max_minutes는")
print("   60분을 넘어도 정상입니다 - 예: practice 기본 30~60분 x hard 배율 1.3 = 약 40~80분)")
print("- unit_name이 시험범위 내용과 맞게 나뉘었는지")
print("- ai_reason이 그럴듯한지 (형식적인 문구만 반복하지 않는지, 과목별로 다른 이유가 나오는지)")
print("- 같은 단원 안에서 제목만 다르고 실제 학습 목표가 겹치는 작업이 있는지")
print("  (예: 같은 개념을 다루는 concept 작업이 제목만 바꿔 두 번 생성된 경우)")
print("- 분포 요약에서 importance/difficulty가 전부 같은 값 하나로 쏠려있지 않은지")
print("  (예: 전부 medium/normal이면 -> 그냥 기본값만 반복, 구분을 못 하고 있다는 뜻)")
print("- 공학수학(계산 위주)이 데이터통신(개념 위주)보다 difficulty가 대체로 높게 나오는지")
print("  (내용 성격에 맞게 실제로 구분해서 판단하는지 확인하는 포인트)")