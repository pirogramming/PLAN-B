# PLAN B

실제 공부 기록에 맞춰 무너진 시험 계획을 다시 계산하는 시험 특화 학습 플래너입니다.

일반 플래너와 달리, 시험일까지 계획이 실제로 가능한지 진단하고 사용자의 실제 공부 속도를 반영하며 계획 실패 시 남은 일정을 자동으로 복구합니다.

## 개발 환경

- Python 3.12
- Django (requirements.txt 참고)

## 실행 방법

```bash
git clone https://github.com/pirogramming/PLAN-B.git
cd PLAN-B
git switch dev

python -m venv venv

# Windows (Git Bash)
source venv/Scripts/activate

# macOS / Linux
source venv/bin/activate

pip install -r requirements.txt
```

`.env.example`을 복사해 `.env`를 생성합니다.

```bash
cp .env.example .env
```

```bash
python manage.py migrate
python manage.py runserver
```

## 앱 구조

- `accounts` — 회원, 사용자 공부 속도 프로필
- `exams` — 시험, 공부 가능시간, 학습 자료, 단원/작업
- `planner` — 날짜별 계획, 진행 기록, 속도 보정, 복구 로직
- `core` — 공통 상수, choices, 예외처리

## Git 규칙

- `main`: 최종 배포 브랜치
- `dev`: 개발 통합 브랜치
- 기능 개발은 Issue 생성 후 별도 브랜치에서 진행
- `dev` 브랜치에 직접 push 금지
- PR은 `dev` 브랜치를 대상으로 생성

브랜치 예시:

- `feature/#12-exam-create`
- `fix/#18-login-error`
- `refactor/#25-planner-service`

## 팀 구성

| 역할 | 담당자 | 담당 영역 | 정확한 담당 범위 |
|---|---|---|---|
| FE1 | 신예원 | 초기 설정·최초 계획 생성 화면 | 시험 등록, 시험일·과목 입력, 날짜별 공부 가능시간 입력, PDF·텍스트 범위 입력, AI 학습 작업 확인·수정, 최초 계획 생성 요청 화면 |
| FE2 | 강보민 | 계획 실행·복구 화면 | 실현 가능성 결과, 날짜별 계획, 오늘의 공부, 완료·일부완료·못함 입력, 복구안 비교, 복구안 선택·적용 결과 화면 |
| BE1 | 이주헌 | 계획·복구 엔진 및 백엔드 통합 | 전체 백엔드 구조 설계, planner 모델, 예상시간 계산, 실현 가능성 판정, 날짜별 일정 생성, 분량 유지형·핵심 집중형 복구 계산, 복구안 적용, 백엔드 기능 통합 |
| BE2 | 장희원 | 계정·시험 데이터·PDF 처리 | 회원가입·로그인, accounts·exams 모델, 시험 CRUD, 날짜별 가능시간 CRUD, 학습자료 업로드·저장, PDF 유효성 검사, PDF 텍스트 추출, StudyUnit·StudyTask CRUD, 사용자 수정값 저장 |
| BE3 | 김유겸 | AI 분석·진행 기록·속도 보정 | 추출된 텍스트를 AI에 전달, 단원·학습 작업 생성, 작업 유형·중요도·깊이·난이도 추천, AI 응답 JSON 검증·예외처리, 공부 진행 결과 저장 기능, 실제 공부 속도 계산 및 speed_factor 보정 |
