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

| 역할 | 담당자 | 담당 범위 |
|---|---|---|
| FE1 |  신예원  | 시험등록/가능시간입력/PDF텍스트입력/AI작업확인수정 화면 |
| FE2 |  강보민  | 실현가능성결과/날짜별계획/오늘의공부/진행결과입력/복구안비교선택 화면 |
| BE1 |  이주헌  | 전체 백엔드 구조설계, planner 모델, 예상시간계산, 실현가능성판정, 날짜별일정생성, 진행기록, 속도보정, 복구로직 |
| BE2 |  장희원  | accounts, exams 모델, 시험/가능시간/단원/작업 CRUD |
| BE3 |  김유겸  | PDF텍스트추출, AI API연동, 단원/작업 구조화, 중요도추천 |
