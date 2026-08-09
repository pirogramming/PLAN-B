from datetime import timedelta
from django.utils import timezone
from django.contrib.auth import get_user_model
from core.choices import (
    ExamPeriodStatus, PriorityLevel, TaskType, TaskDepth,
    TaskDifficulty, ProgressStatus,
)
from exams.models import ExamPeriod, Exam, AvailableTime, StudyTask
from planner.models import DailyPlan, DailyPlanItem, ProgressLog
from planner.services.progress_recorder import finalize_daily_plan

User = get_user_model()
user = User.objects.first()
if not user:
    raise SystemExit("로컬 DB에 사용자가 없습니다. 먼저 로그인/회원가입으로 계정 하나 만들어주세요.")

today = timezone.localdate()

exam_period = ExamPeriod.objects.create(
    user=user,
    title="복구안 테스트용 시험기간",
    start_date=today - timedelta(days=3),
    end_date=today + timedelta(days=10),
    status=ExamPeriodStatus.ACTIVE,
)

exam = Exam.objects.create(
    exam_period=exam_period,
    subject_name="테스트 과목",
    exam_date=today + timedelta(days=7),
    priority=PriorityLevel.HIGH,
    speed_factor=1.0,
)

task_core = StudyTask.objects.create(
    exam=exam, title="핵심 개념 정리", task_type=TaskType.CONCEPT,
    importance=PriorityLevel.HIGH, depth=TaskDepth.CORE,
    difficulty=TaskDifficulty.NORMAL,
    estimated_min_minutes=20, estimated_max_minutes=40, order=1,
)
task_optional1 = StudyTask.objects.create(
    exam=exam, title="선택 문제풀이 1", task_type=TaskType.PRACTICE,
    importance=PriorityLevel.LOW, depth=TaskDepth.OPTIONAL,
    difficulty=TaskDifficulty.NORMAL,
    estimated_min_minutes=30, estimated_max_minutes=60, order=2,
)
task_optional2 = StudyTask.objects.create(
    exam=exam, title="선택 문제풀이 2", task_type=TaskType.PRACTICE,
    importance=PriorityLevel.LOW, depth=TaskDepth.OPTIONAL,
    difficulty=TaskDifficulty.NORMAL,
    estimated_min_minutes=30, estimated_max_minutes=60, order=3,
)
task_basic = StudyTask.objects.create(
    exam=exam, title="기본 복습", task_type=TaskType.REVIEW,
    importance=PriorityLevel.LOW, depth=TaskDepth.BASIC,
    difficulty=TaskDifficulty.NORMAL,
    estimated_min_minutes=15, estimated_max_minutes=30, order=4,
)

for i in range(1, 7):
    AvailableTime.objects.create(
        exam_period=exam_period,
        date=today + timedelta(days=i),
        available_minutes=60,
    )

daily_plan = DailyPlan.objects.create(
    exam_period=exam_period, date=today,
    available_minutes=120, planned_minutes=190,
)

items = []
for order, task in enumerate([task_core, task_optional1, task_optional2, task_basic], start=1):
    items.append(DailyPlanItem.objects.create(
        daily_plan=daily_plan, study_task=task,
        planned_minutes=task.estimated_max_minutes, order=order,
    ))

ProgressLog.objects.create(
    daily_plan_item=items[0], progress_status=ProgressStatus.DONE,
    actual_minutes=35, completion_percent=100,
)

result = finalize_daily_plan(daily_plan, mark_unrecorded_as_not_done=True)
print("needs_recovery:", result["needs_recovery"])

recovery_plans = result["recovery_plans"]
if recovery_plans:
    mv = recovery_plans.get("maintain_volume")
    cf = recovery_plans.get("core_focus")
    print("maintain_volume:", mv)
    print("core_focus:", cf)
    group_id = (mv or cf).recovery_group_id
    print(f"확인 URL: http://127.0.0.1:8000/planner/recovery/{group_id}/")
else:
    print("recovery_plans가 비어있습니다 - 복구안이 생성되지 않았습니다.")
