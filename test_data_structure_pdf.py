"""
자료구조_7.pdf OCR 추출 텍스트로 실제 Gemini API 품질 테스트.
- 원문이 영어라 응답도 한국어로 잘 나오는지
- 코드 스니펫(C++)이 개념 설명이랑 안 헷갈리고 잘 나뉘는지
- OCR 노이즈("101 30 기 410" 같은 깨진 글자)가 판단을 방해하는지
- source_pages(리스트)가 실제 슬라이드 페이지 번호랑 맞게, 관련된 페이지를
  빠짐없이 다 담아서 나오는지

실행법 (manage.py와 같은 폴더에서):
    python manage.py shell -c "exec(open('test_data_structure_pdf.py', encoding='utf-8').read())"

주의:
- .env에서 AI_MOCK_MODE=False, GOOGLE_API_KEY 실제 키 확인 후 실행할 것
- '<' 리다이렉션으로 실행하지 말 것 (IndentationError 발생)
- "--- 페이지 N ---" 경계 표시가 있는 텍스트 파일을 써야 source_pages가 채워진다.
"""
import datetime
from exams.services.task_extractor import fetch_extracted_tasks
from django.conf import settings

if getattr(settings, "AI_MOCK_MODE", True):
    print("!! AI_MOCK_MODE=True 상태입니다. .env에서 False로 바꾸고 다시 실행하세요.")
    raise SystemExit(1)

# 같은 폴더에 저장해둔 OCR 텍스트 파일을 읽어온다.
# (자료구조_7_ocr_text.txt를 manage.py와 같은 폴더에 두거나, 아래 경로를 직접 수정)
with open("자료구조_7_ocr_text.txt", encoding="utf-8") as f:
    source_text = f.read()

has_page_markers = "--- 페이지" in source_text
print(f"입력 텍스트 길이: {len(source_text)}자")
print(f"페이지 경계 표시 포함 여부: {has_page_markers}")
if not has_page_markers:
    print("!! 페이지 표시가 없는 텍스트입니다 - source_pages는 전부 빈 리스트로 나오는 게 정상입니다.")
print("=" * 60)

# Exam 객체 없이 build_prompt에 필요한 값만 흉내낸다.
class FakeExam:
    subject_name = "자료구조"
    exam_date = datetime.date(2026, 9, 1)

tasks = fetch_extracted_tasks(FakeExam(), source_text)

print(f"생성된 작업 개수: {len(tasks)}\n")

for t in tasks:
    if t.source_pages:
        pages_str = ", ".join(str(p) for p in t.source_pages) + "페이지"
    else:
        pages_str = "페이지 정보 없음"
    print(f"- [{t.unit_name}] {t.title}  ({pages_str})")
    print(f"    유형/중요도/깊이/난이도: {t.task_type}/{t.importance}/{t.depth}/{t.difficulty}")
    print(f"    추천 이유: {t.ai_reason}")
    print()

# 분포 요약
from collections import Counter
print("=" * 60)
print("전체 분포 요약")
print("=" * 60)
print("중요도(importance):", dict(Counter(t.importance for t in tasks)))
print("깊이(depth):        ", dict(Counter(t.depth for t in tasks)))
print("난이도(difficulty):  ", dict(Counter(t.difficulty for t in tasks)))
print("작업유형(task_type): ", dict(Counter(t.task_type for t in tasks)))

# source_pages 관련 요약
pages_filled = [t for t in tasks if t.source_pages]
pages_missing = [t for t in tasks if not t.source_pages]
page_counts = [len(t.source_pages) for t in tasks if t.source_pages]
print(f"source_pages 채워짐: {len(pages_filled)}/{len(tasks)}개")
if page_counts:
    print(f"작업당 페이지 개수: 최소 {min(page_counts)}개 / 최대 {max(page_counts)}개 / 평균 {sum(page_counts)/len(page_counts):.1f}개")
if pages_missing and has_page_markers:
    print("  -> 페이지 표시가 있는데도 비어있는 작업:")
    for t in pages_missing:
        print(f"     - [{t.unit_name}] {t.title}")

print("\n확인 포인트:")
print("- unit_name이 실제 슬라이드 챕터(Circular/Doubly/Header/Sorted/Circular Doubly)와 맞게 나뉘었는지")
print("- 원문이 영어인데 title/ai_reason이 자연스러운 한국어로 나왔는지")
print("- 코드 구현(InsertFront/DeleteRear 등)과 개념 설명이 서로 다른 작업으로 잘 분리됐는지")
print("- OCR 노이즈(깨진 문자, 도형 라벨 텍스트)가 엉뚱한 unit_name이나 title로 새어나오지 않았는지")
print("- 이론(장단점, 시간복잡도) vs 구현(InsertFront 코드)이 다른 task_type으로 구분됐는지")
print("- source_pages가 그 작업과 실제로 관련된 페이지를 빠짐없이 담고 있는지")
print("  (대표 페이지 1개만이 아니라, 예: 'Circular Linked List 개념'이 4,5페이지 둘 다 걸쳐있으면 [4, 5]로 나오는지)")
print("- 페이지 번호가 그 작업 제목/단원이랑 실제로 맞는 범위인지 (엉뚱한 페이지가 안 섞였는지)")