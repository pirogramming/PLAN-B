from django.test import TestCase
from datetime import timedelta
from django.utils import timezone as django_timezone
from unittest.mock import patch
from planner.services.recovery import RecoveryPlanInvalidDataError
from django.urls import reverse
import uuid
from planner.models import RecoveryPlanItem
from core.choices import RecoveryPlanStatus, RecoveryActionType
from planner.services.progress_recorder import (
    finalize_daily_plan,
    record_progress,
    DailyPlanAlreadyFinalizedError,
    FutureDailyPlanFinalizeError,
    FinalizedDailyPlanEditError,
)
from planner.models import RecoveryPlan
from core.choices import RecoveryType
from exams.models import AvailableTime
from planner.services.progress_recorder import IncompleteProgressError
from planner.services.time_estimator import estimate_task_minutes, round_up_to_five

from planner.services.recovery import (
    generate_recovery_options,
    apply_recovery_plan,
    RecoveryPlanAlreadyProcessedError,
    RecoveryPlanStaleError,
    _get_carry_along_items,
)
from planner.services.recovery import (
    needs_recovery_retry,
    retry_recovery_generation,
    RecoveryRetryNotNeededError,
)
# Create your tests here.
from planner.services.feasibility_checker import (
    calculate_feasibility,
    POSSIBLE,
    RISKY,
    IMPOSSIBLE,
)


class CalculateFeasibilityTests(TestCase):
    def test_possible_when_available_meets_max(self):
        result = calculate_feasibility(
            required_min_minutes=600,
            required_max_minutes=900,
            available_minutes=900,
        )
        self.assertEqual(result["status"], POSSIBLE)
        self.assertEqual(result["shortage_minutes"], 0)

    def test_possible_when_available_exceeds_max(self):
        result = calculate_feasibility(600, 900, 1200)
        self.assertEqual(result["status"], POSSIBLE)
        self.assertEqual(result["shortage_minutes"], 0)

    def test_risky_when_between_min_and_max(self):
        result = calculate_feasibility(
            required_min_minutes=900,
            required_max_minutes=1200,
            available_minutes=1050,
        )
        self.assertEqual(result["status"], RISKY)
        self.assertEqual(result["shortage_minutes"], 150)

    def test_risky_at_exact_min_boundary(self):
        result = calculate_feasibility(900, 1200, 900)
        self.assertEqual(result["status"], RISKY)
        self.assertEqual(result["shortage_minutes"], 300)

    def test_impossible_when_below_min(self):
        result = calculate_feasibility(
            required_min_minutes=900,
            required_max_minutes=1200,
            available_minutes=500,
        )
        self.assertEqual(result["status"], IMPOSSIBLE)
        self.assertEqual(result["shortage_minutes"], 400)

    def test_response_shape_matches_api_spec(self):
        result = calculate_feasibility(900, 1200, 1050)
        self.assertEqual(
            set(result.keys()),
            {
                "status",
                "required_min_minutes",
                "required_recommended_minutes",
                "available_minutes",
                "shortage_minutes",
            },
        )

from planner.services.time_estimator import (
    estimate_task_minutes,
    round_up_to_five,
)


class RoundUpToFiveTests(TestCase):
    def test_exact_multiple_of_five(self):
        self.assertEqual(round_up_to_five(30), 30)

    def test_rounds_up(self):
        self.assertEqual(round_up_to_five(28.6), 30)
        self.assertEqual(round_up_to_five(21), 25)

    def test_zero(self):
        self.assertEqual(round_up_to_five(0), 0)


class EstimateTaskMinutesTests(TestCase):
    def test_concept_normal_speed_one(self):
        min_m, max_m = estimate_task_minutes("concept", "normal", 1.0)
        self.assertEqual((min_m, max_m), (20, 40))

    def test_practice_hard_speed_one(self):
        min_m, max_m = estimate_task_minutes("practice", "hard", 1.0)
        self.assertEqual((min_m, max_m), (40, 80))

    def test_review_easy_speed_one(self):
        min_m, max_m = estimate_task_minutes("review", "easy", 1.0)
        self.assertEqual((min_m, max_m), (15, 25))

    def test_concept_hard_with_speed_factor(self):
        min_m, max_m = estimate_task_minutes("concept", "hard", 1.1)
        self.assertEqual((min_m, max_m), (30, 60))

    def test_unknown_task_type_falls_back_to_custom(self):
        min_m, max_m = estimate_task_minutes("weird_type", "normal", 1.0)
        self.assertEqual((min_m, max_m), (20, 40))

    def test_unknown_difficulty_falls_back_to_normal(self):
        min_m, max_m = estimate_task_minutes("concept", "weird_difficulty", 1.0)
        self.assertEqual((min_m, max_m), (20, 40))

    def test_speed_factor_below_one_makes_faster(self):
        min_m, max_m = estimate_task_minutes("practice", "normal", 0.9)
        self.assertEqual((min_m, max_m), (30, 55))


from planner.services.speed_calibrator import calculate_speed_factor


class CalculateSpeedFactorTests(TestCase):
    """
    calculate_speed_factor()는 실제 ORM 객체(ProgressLog, StudyTask 등)의
    속성에 접근하므로, 여기서는 필요한 속성만 가진 경량 스텁 객체로 테스트한다.
    """

    class _StubTask:
        def __init__(self, estimated_min_minutes, estimated_max_minutes):
            self.estimated_min_minutes = estimated_min_minutes
            self.estimated_max_minutes = estimated_max_minutes

    class _StubItem:
        def __init__(self, planned_minutes):
            self.planned_minutes = planned_minutes

    class _StubLog:
        def __init__(self, progress_status, completion_percent, actual_minutes, planned_minutes):
            self.progress_status = progress_status
            self.completion_percent = completion_percent
            self.actual_minutes = actual_minutes
            self.daily_plan_item = CalculateSpeedFactorTests._StubItem(planned_minutes)


    def _log(self, status, percent, actual_minutes, planned_minutes=30):
        return self._StubLog(status, percent, actual_minutes, planned_minutes)

    def test_no_logs_returns_default(self):
        self.assertEqual(calculate_speed_factor([]), 1.0)

    def test_not_done_logs_excluded(self):
        logs = [self._log("not_done", 0, 0)]
        self.assertEqual(calculate_speed_factor(logs), 1.0)

    def test_faster_than_expected_lowers_factor(self):
        # 기준 예상시간 (20+40)/2=30, 실제 15분 -> ratio=0.5
        logs = [self._log("done", 100, 15)]
        result = calculate_speed_factor(logs)
        self.assertLess(result, 1.0)

    def test_slower_than_expected_raises_factor(self):
        # 기준 30분, 실제 60분 -> ratio=2.0
        logs = [self._log("done", 100, 60)]
        result = calculate_speed_factor(logs)
        self.assertGreater(result, 1.0)

    def test_partial_completion_uses_proportional_expected_time(self):
        # 기준 30분의 50% = 15분 예상, 실제 15분 -> ratio=1.0 -> factor 그대로 1.0
        logs = [self._log("partial", 50, 15)]
        self.assertEqual(calculate_speed_factor(logs), 1.0)

    def test_recalculation_is_idempotent_not_cumulative(self):
        # 같은 로그 하나만 있을 때, 두 번 계산해도 같은 결과여야 함
        # (수정 후 재계산 시 이전 값이 이중 반영되지 않는지 확인)
        logs = [self._log("done", 100, 60)]
        first = calculate_speed_factor(logs)
        second = calculate_speed_factor(logs)
        self.assertEqual(first, second)

    def test_result_is_clamped_within_bounds(self):
        logs = [self._log("done", 100, 1000)]  # 극단적으로 느림
        result = calculate_speed_factor(logs)
        self.assertLessEqual(result, 1.5)

    def test_result_is_clamped_at_lower_bound(self):
        logs = [self._log("done", 100, 1)]  # 극단적으로 빠름
        result = calculate_speed_factor(logs)
        self.assertGreaterEqual(result, 0.7)


from datetime import date

from planner.services.progress_recorder import (
    record_progress,
    normalize_completion_percent,
    normalize_actual_minutes,
    determine_daily_plan_status,
)
from planner.models import ProgressLog


class NormalizeCompletionPercentTests(TestCase):
    def test_done_forces_100(self):
        self.assertEqual(normalize_completion_percent("done", 50), 100)

    def test_not_done_forces_0(self):
        self.assertEqual(normalize_completion_percent("not_done", 50), 0)

    def test_partial_within_range_passes(self):
        self.assertEqual(normalize_completion_percent("partial", 60), 60)

    def test_partial_out_of_range_raises(self):
        with self.assertRaises(ValueError):
            normalize_completion_percent("partial", 0)
        with self.assertRaises(ValueError):
            normalize_completion_percent("partial", 100)

    def test_partial_missing_raises(self):
        with self.assertRaises(ValueError):
            normalize_completion_percent("partial", None)


class NormalizeActualMinutesTests(TestCase):
    def test_done_requires_positive_minutes(self):
        self.assertEqual(normalize_actual_minutes("done", 30), 30)
        with self.assertRaises(ValueError):
            normalize_actual_minutes("done", 0)
        with self.assertRaises(ValueError):
            normalize_actual_minutes("done", None)

    def test_not_done_defaults_to_zero(self):
        self.assertEqual(normalize_actual_minutes("not_done", None), 0)


class DetermineDailyPlanStatusTests(TestCase):
    def test_empty_returns_planned(self):
        self.assertEqual(determine_daily_plan_status([]), "planned")

    def test_all_completed_returns_completed(self):
        self.assertEqual(
            determine_daily_plan_status(["completed", "completed"]), "completed"
        )

    def test_any_at_risk_returns_at_risk(self):
        self.assertEqual(
            determine_daily_plan_status(["completed", "at_risk"]), "at_risk"
        )

    def test_partial_completed_returns_in_progress(self):
        self.assertEqual(
            determine_daily_plan_status(["completed", "planned"]), "in_progress"
        )

    def test_all_planned_returns_planned(self):
        self.assertEqual(
            determine_daily_plan_status(["planned", "planned"]), "planned"
        )


class RecordProgressTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        from exams.models import Exam, ExamPeriod, StudyTask
        from planner.models import DailyPlan, DailyPlanItem

        User = get_user_model()
        self.user = User.objects.create_user(
            username="tester2", email="tester2@example.com", password="pass1234"
        )
        self.exam_period = ExamPeriod.objects.create(
            user=self.user,
            title="테스트 시험기간",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 20),
        )
        self.exam = Exam.objects.create(
            exam_period=self.exam_period,
            subject_name="테스트 과목",
            exam_date=date(2026, 8, 10),
        )
        self.task = StudyTask.objects.create(
            exam=self.exam,
            title="테스트 작업",
            importance="high",
            order=1,
            estimated_min_minutes=20,
            estimated_max_minutes=40,
            is_confirmed=True,
        )
        self.daily_plan = DailyPlan.objects.create(
            exam_period=self.exam_period,
            date=date(2026, 8, 1),
            available_minutes=60,
            planned_minutes=40,
        )
        self.item = DailyPlanItem.objects.create(
            daily_plan=self.daily_plan,
            study_task=self.task,
            planned_minutes=40,
            order=1,
        )

    def test_creates_progress_log(self):
        result = record_progress(
            daily_plan_item=self.item,
            status="done",
            actual_minutes=35,
        )
        self.assertEqual(ProgressLog.objects.count(), 1)
        self.assertEqual(result["progress_log"].completion_percent, 100)

    def test_resubmitting_updates_not_duplicates(self):
        record_progress(daily_plan_item=self.item, status="partial", actual_minutes=20, completion_percent=50)
        record_progress(daily_plan_item=self.item, status="done", actual_minutes=40)

        self.assertEqual(ProgressLog.objects.count(), 1)
        log = ProgressLog.objects.first()
        self.assertEqual(log.progress_status, "done")
        self.assertEqual(log.completion_percent, 100)

    def test_daily_plan_item_status_synced(self):
        record_progress(daily_plan_item=self.item, status="done", actual_minutes=35)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, "completed")

    def test_daily_plan_status_recalculated(self):
        record_progress(daily_plan_item=self.item, status="done", actual_minutes=35)
        self.daily_plan.refresh_from_db()
        self.assertEqual(self.daily_plan.status, "completed")

    def test_speed_factor_updated_on_exam(self):
        record_progress(daily_plan_item=self.item, status="done", actual_minutes=60)
        self.exam.refresh_from_db()
        self.assertNotEqual(self.exam.speed_factor, 1.0)

    def test_speed_factor_not_double_counted_on_resubmit(self):
        record_progress(daily_plan_item=self.item, status="done", actual_minutes=60)
        self.exam.refresh_from_db()
        first_factor = self.exam.speed_factor

        # 같은 항목을 같은 값으로 다시 제출해도 factor가 변하지 않아야 함
        record_progress(daily_plan_item=self.item, status="done", actual_minutes=60)
        self.exam.refresh_from_db()
        second_factor = self.exam.speed_factor

        self.assertEqual(first_factor, second_factor)

    def test_invalid_partial_percent_raises(self):
        with self.assertRaises(ValueError):
            record_progress(
                daily_plan_item=self.item,
                status="partial",
                actual_minutes=20,
                completion_percent=150,
            )

class RecalculateSpeedFactorOrderingTests(TestCase):
    """
    같은 날짜에 ProgressLog가 여러 개일 때, EMA 계산 순서가
    daily_plan_item__order 기준으로 결정적인지 확인한다.

    recalculate_speed_factor()가 만약 date만으로 정렬하고 동률을
    DB 기본 순서(보통 삽입/pk 순서)에 맡긴다면, 로그를 만드는 순서에
    따라 결과가 달라진다. 이 테스트는 order=2인 항목의 로그를 먼저
    만들어(pk가 더 작게) 일부러 "삽입 순서 != order 순서"인 상황을
    만들고, 그래도 daily_plan_item__order 기준(1번 먼저 -> 2번 나중)
    으로 계산됐는지를 정확한 기대값으로 검증한다.
    """

    def setUp(self):
        from django.contrib.auth import get_user_model
        from exams.models import Exam, ExamPeriod, StudyTask
        from planner.models import DailyPlan, DailyPlanItem

        User = get_user_model()
        self.user = User.objects.create_user(
            username="tester3", email="tester3@example.com", password="pass1234"
        )
        self.exam_period = ExamPeriod.objects.create(
            user=self.user,
            title="테스트 시험기간",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 20),
        )
        self.exam = Exam.objects.create(
            exam_period=self.exam_period,
            subject_name="테스트 과목",
            exam_date=date(2026, 8, 10),
        )
        self.daily_plan = DailyPlan.objects.create(
            exam_period=self.exam_period,
            date=date(2026, 8, 1),
            available_minutes=90,
            planned_minutes=60,
        )

        def _make_item(order, planned_minutes):
            task = StudyTask.objects.create(
                exam=self.exam,
                title=f"작업 {order}",
                importance="high",
                order=order,
                estimated_min_minutes=planned_minutes,
                estimated_max_minutes=planned_minutes,
                is_confirmed=True,
            )
            return DailyPlanItem.objects.create(
                daily_plan=self.daily_plan,
                study_task=task,
                planned_minutes=planned_minutes,
                order=order,
            )

        self.item_order1 = _make_item(order=1, planned_minutes=30)
        self.item_order2 = _make_item(order=2, planned_minutes=30)

    def test_same_date_logs_processed_by_daily_plan_item_order(self):
        from planner.services.speed_calibrator import recalculate_speed_factor

        # 일부러 order=2 항목의 로그를 먼저 만든다 (pk가 더 작아짐).
        # date만으로 정렬하면 이 pk 순서(2번 먼저)로 계산되지만,
        # 올바른 기준은 daily_plan_item__order(1번 먼저)여야 한다.
        ProgressLog.objects.create(
            daily_plan_item=self.item_order2,
            progress_status="done",
            actual_minutes=60,      # ratio = 60/30 = 2.0
            completion_percent=100,
        )
        ProgressLog.objects.create(
            daily_plan_item=self.item_order1,
            progress_status="done",
            actual_minutes=15,      # ratio = 15/30 = 0.5
            completion_percent=100,
        )

        result = recalculate_speed_factor(self.exam)

        # order=1(ratio 0.5) 먼저 -> order=2(ratio 2.0) 나중 순서로 계산됐을 때
        # 나오는 정확한 기대값. (1.0*0.7 + 0.5*0.3) = 0.85
        # (0.85*0.7 + 2.0*0.3) = 1.195 -> clamp(0.7, 1.5) 안쪽이라 그대로.
        self.assertEqual(result, 1.195)


from planner.services.scheduler import (
    TaskInput,
    AvailableTimeInput,
    allocate_tasks_to_days,
)


class AllocateTasksToDaysTests(TestCase):
    def test_places_task_on_earliest_available_day(self):
        tasks = [
            TaskInput(id=1, exam_date=date(2026, 8, 10), importance="high", order=1, estimated_max_minutes=60),
        ]
        available_times = [
            AvailableTimeInput(date=date(2026, 8, 1), available_minutes=60),
            AvailableTimeInput(date=date(2026, 8, 2), available_minutes=60),
        ]
        result = allocate_tasks_to_days(tasks, available_times)
        self.assertEqual(result["allocations"], [
            {"task_id": 1, "date": date(2026, 8, 1), "allocated_minutes": 60},
        ])
        self.assertEqual(result["unallocated_tasks"], [])

    def test_sorted_by_exam_date_first(self):
        tasks = [
            TaskInput(id=1, exam_date=date(2026, 8, 15), importance="high", order=1, estimated_max_minutes=60),
            TaskInput(id=2, exam_date=date(2026, 8, 10), importance="low", order=1, estimated_max_minutes=60),
        ]
        available_times = [
            AvailableTimeInput(date=date(2026, 8, 1), available_minutes=60),
            AvailableTimeInput(date=date(2026, 8, 2), available_minutes=60),
        ]
        result = allocate_tasks_to_days(tasks, available_times)
        # exam_date가 빠른 task_id=2가 먼저 배치되어 8/1을 차지해야 함
        first = next(a for a in result["allocations"] if a["task_id"] == 2)
        second = next(a for a in result["allocations"] if a["task_id"] == 1)
        self.assertEqual(first["date"], date(2026, 8, 1))
        self.assertEqual(second["date"], date(2026, 8, 2))

    def test_sorted_by_importance_when_same_exam_date(self):
        tasks = [
            TaskInput(id=1, exam_date=date(2026, 8, 10), importance="low", order=1, estimated_max_minutes=60),
            TaskInput(id=2, exam_date=date(2026, 8, 10), importance="high", order=1, estimated_max_minutes=60),
        ]
        available_times = [
            AvailableTimeInput(date=date(2026, 8, 1), available_minutes=60),
            AvailableTimeInput(date=date(2026, 8, 2), available_minutes=60),
        ]
        result = allocate_tasks_to_days(tasks, available_times)
        high_alloc = next(a for a in result["allocations"] if a["task_id"] == 2)
        low_alloc = next(a for a in result["allocations"] if a["task_id"] == 1)
        self.assertEqual(high_alloc["date"], date(2026, 8, 1))
        self.assertEqual(low_alloc["date"], date(2026, 8, 2))

    def test_sorted_by_order_when_same_exam_date_and_importance(self):
        tasks = [
            TaskInput(id=1, exam_date=date(2026, 8, 10), importance="high", order=2, estimated_max_minutes=60),
            TaskInput(id=2, exam_date=date(2026, 8, 10), importance="high", order=1, estimated_max_minutes=60),
        ]
        available_times = [
            AvailableTimeInput(date=date(2026, 8, 1), available_minutes=60),
            AvailableTimeInput(date=date(2026, 8, 2), available_minutes=60),
        ]
        result = allocate_tasks_to_days(tasks, available_times)
        order1_alloc = next(a for a in result["allocations"] if a["task_id"] == 2)
        order2_alloc = next(a for a in result["allocations"] if a["task_id"] == 1)
        self.assertEqual(order1_alloc["date"], date(2026, 8, 1))
        self.assertEqual(order2_alloc["date"], date(2026, 8, 2))

    def test_not_placed_on_or_after_exam_date(self):
        tasks = [
            TaskInput(id=1, exam_date=date(2026, 8, 2), importance="high", order=1, estimated_max_minutes=60),
        ]
        available_times = [
            AvailableTimeInput(date=date(2026, 8, 1), available_minutes=60),
            AvailableTimeInput(date=date(2026, 8, 2), available_minutes=60),  # 시험 당일, 배치 금지
            AvailableTimeInput(date=date(2026, 8, 3), available_minutes=60),  # 시험 이후, 배치 금지
        ]
        result = allocate_tasks_to_days(tasks, available_times)
        self.assertEqual(result["allocations"], [
            {"task_id": 1, "date": date(2026, 8, 1), "allocated_minutes": 60},
        ])

    def test_different_exam_dates_per_subject_respected(self):
        tasks = [
            TaskInput(id=1, exam_date=date(2026, 8, 5), importance="high", order=1, estimated_max_minutes=60),  # 수학
            TaskInput(id=2, exam_date=date(2026, 8, 20), importance="high", order=1, estimated_max_minutes=60),  # 영어
        ]
        available_times = [
            AvailableTimeInput(date=date(2026, 8, 4), available_minutes=60),
            AvailableTimeInput(date=date(2026, 8, 10), available_minutes=60),
        ]
        result = allocate_tasks_to_days(tasks, available_times)
        math_alloc = next(a for a in result["allocations"] if a["task_id"] == 1)
        english_alloc = next(a for a in result["allocations"] if a["task_id"] == 2)
        # 수학은 시험일(8/5) 이전인 8/4에만 배치 가능
        self.assertEqual(math_alloc["date"], date(2026, 8, 4))
        # 영어는 8/4에 자리가 없으면(수학이 이미 차지) 8/10에 배치
        self.assertEqual(english_alloc["date"], date(2026, 8, 10))

    def test_skips_day_with_insufficient_remaining_minutes(self):
        tasks = [
            TaskInput(id=1, exam_date=date(2026, 8, 10), importance="high", order=1, estimated_max_minutes=50),
        ]
        available_times = [
            AvailableTimeInput(date=date(2026, 8, 1), available_minutes=30),  # 부족
            AvailableTimeInput(date=date(2026, 8, 2), available_minutes=50),  # 충분
        ]
        result = allocate_tasks_to_days(tasks, available_times)
        self.assertEqual(result["allocations"], [
            {"task_id": 1, "date": date(2026, 8, 2), "allocated_minutes": 50},
        ])

    def test_does_not_split_task(self):
        tasks = [
            TaskInput(id=1, exam_date=date(2026, 8, 10), importance="high", order=1, estimated_max_minutes=100),
        ]
        available_times = [
            AvailableTimeInput(date=date(2026, 8, 1), available_minutes=60),
            AvailableTimeInput(date=date(2026, 8, 2), available_minutes=60),
        ]
        # 어느 날짜도 100분을 단독으로 수용 못 함 -> 분할 없이 미배치 처리
        result = allocate_tasks_to_days(tasks, available_times)
        self.assertEqual(result["allocations"], [])
        self.assertEqual(result["unallocated_tasks"], [1])

    def test_unallocated_task_is_reported_not_dropped(self):
        tasks = [
            TaskInput(id=1, exam_date=date(2026, 8, 2), importance="high", order=1, estimated_max_minutes=60),
        ]
        available_times = [
            AvailableTimeInput(date=date(2026, 8, 1), available_minutes=10),  # 부족
        ]
        result = allocate_tasks_to_days(tasks, available_times)
        self.assertEqual(result["allocations"], [])
        self.assertEqual(result["unallocated_tasks"], [1])

    def test_remaining_minutes_decrease_after_allocation(self):
        tasks = [
            TaskInput(id=1, exam_date=date(2026, 8, 10), importance="high", order=1, estimated_max_minutes=40),
            TaskInput(id=2, exam_date=date(2026, 8, 10), importance="high", order=2, estimated_max_minutes=40),
        ]
        available_times = [
            AvailableTimeInput(date=date(2026, 8, 1), available_minutes=60),
        ]
        result = allocate_tasks_to_days(tasks, available_times)
        # 첫 작업이 40분 차지하면 남은 20분으로는 두 번째(40분) 작업이 못 들어감
        self.assertEqual(len(result["allocations"]), 1)
        self.assertEqual(result["unallocated_tasks"], [2])

    def test_best_fit_avoids_fragmentation_that_first_fit_would_miss(self):
        # 2,2,7,7,7 / 하루 10분씩 3일: 7+2 / 7+2 / 7 로 전부 배치 가능한
        # 조합이 존재하는데, 작은 작업부터 넣는 First-Fit은 공간을 파편화시켜
        # 마지막 7분 작업 하나를 미배치로 만든다. Best-Fit Decreasing이면
        # 전부 들어가야 한다.
        exam_date = date(2026, 8, 10)
        tasks = [
            TaskInput(id=1, exam_date=exam_date, importance="medium", order=1, estimated_max_minutes=2),
            TaskInput(id=2, exam_date=exam_date, importance="medium", order=2, estimated_max_minutes=2),
            TaskInput(id=3, exam_date=exam_date, importance="medium", order=3, estimated_max_minutes=7),
            TaskInput(id=4, exam_date=exam_date, importance="medium", order=4, estimated_max_minutes=7),
            TaskInput(id=5, exam_date=exam_date, importance="medium", order=5, estimated_max_minutes=7),
        ]
        available_times = [
            AvailableTimeInput(date=date(2026, 8, 1), available_minutes=10),
            AvailableTimeInput(date=date(2026, 8, 2), available_minutes=10),
            AvailableTimeInput(date=date(2026, 8, 3), available_minutes=10),
        ]
        result = allocate_tasks_to_days(tasks, available_times)
        self.assertEqual(result["unallocated_tasks"], [])
        self.assertEqual(len(result["allocations"]), 5)

    def test_larger_task_scheduled_before_smaller_within_same_tier(self):
        exam_date = date(2026, 8, 10)
        tasks = [
            TaskInput(id=1, exam_date=exam_date, importance="high", order=1, estimated_max_minutes=3),
            TaskInput(id=2, exam_date=exam_date, importance="high", order=2, estimated_max_minutes=8),
        ]
        available_times = [
            AvailableTimeInput(date=date(2026, 8, 1), available_minutes=8),
            AvailableTimeInput(date=date(2026, 8, 2), available_minutes=10),
        ]
        result = allocate_tasks_to_days(tasks, available_times)
        self.assertEqual(
            result["allocations"],
            [
                {"task_id": 2, "date": date(2026, 8, 1), "allocated_minutes": 8},
                {"task_id": 1, "date": date(2026, 8, 2), "allocated_minutes": 3},
            ],
        )
        self.assertEqual(result["unallocated_tasks"], [])

    def test_best_fit_prefers_day_with_less_remaining_time(self):
        tasks = [
            TaskInput(id=1, exam_date=date(2026, 8, 10), importance="high", order=1, estimated_max_minutes=5),
        ]
        available_times = [
            AvailableTimeInput(date=date(2026, 8, 1), available_minutes=10),
            AvailableTimeInput(date=date(2026, 8, 2), available_minutes=6),
        ]
        result = allocate_tasks_to_days(tasks, available_times)
        self.assertEqual(result["allocations"][0]["date"], date(2026, 8, 2))


    def test_core_depth_scheduled_before_larger_non_core_task(self):
        # 같은 시험일·중요도에서는 depth(core > basic > optional)가
        # 크기(estimated_max_minutes)보다 우선한다. core 작업이 작아도
        # optional의 큰 작업보다 먼저 자리를 차지해야 한다.
        exam_date = date(2026, 8, 10)
        tasks = [
            TaskInput(id=1, exam_date=exam_date, importance="high", order=1, estimated_max_minutes=8, depth="optional"),
            TaskInput(id=2, exam_date=exam_date, importance="high", order=2, estimated_max_minutes=3, depth="core"),
        ]
        available_times = [
            AvailableTimeInput(date=date(2026, 8, 1), available_minutes=8),
            AvailableTimeInput(date=date(2026, 8, 2), available_minutes=10),
        ]
        result = allocate_tasks_to_days(tasks, available_times)
        self.assertEqual(
            result["allocations"],
            [
                {"task_id": 2, "date": date(2026, 8, 1), "allocated_minutes": 3},
                {"task_id": 1, "date": date(2026, 8, 2), "allocated_minutes": 8},
            ],
        )

    def test_default_depth_is_basic_when_not_specified(self):
        task = TaskInput(id=1, exam_date=date(2026, 8, 10), importance="high", order=1, estimated_max_minutes=10)
        self.assertEqual(task.depth, "basic")


from planner.services.schedule_generator import (
    generate_schedule,
    ScheduleAlreadyExistsError,
    ScheduleHasProgressError,
    UnallocatedTasksError,
)
from planner.models import DailyPlan, DailyPlanItem, ProgressLog


class GenerateScheduleTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        from exams.models import Exam, ExamPeriod, StudyTask

        User = get_user_model()
        self.user = User.objects.create_user(
            username="tester", email="tester@example.com", password="pass1234"
        )
        self.exam_period = ExamPeriod.objects.create(
            user=self.user,
            title="테스트 시험기간",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 20),
        )
        self.exam = Exam.objects.create(
            exam_period=self.exam_period,
            subject_name="테스트 과목",
            exam_date=date(2026, 8, 10),
        )

    def _make_task(self, exam=None, importance="high", order=1, estimated_max_minutes=60):
        from exams.models import StudyTask
        return StudyTask.objects.create(
            exam=exam or self.exam,
            title="테스트 작업",
            importance=importance,
            order=order,
            estimated_min_minutes=estimated_max_minutes,
            estimated_max_minutes=estimated_max_minutes,
            is_confirmed=True,
        )

    def _fake_available_time(self, day, minutes):
        class _AT:
            pass
        at = _AT()
        at.date = day
        at.available_minutes = minutes
        return at

    def test_saves_daily_plan_and_items(self):
        tasks = [self._make_task()]
        available_times = [self._fake_available_time(date(2026, 8, 1), 60)]

        result = generate_schedule(
            exam_period=self.exam_period,
            study_tasks=tasks,
            available_times=available_times,
        )

        self.assertEqual(result["created_item_count"], 1)
        self.assertEqual(DailyPlan.objects.filter(exam_period=self.exam_period).count(), 1)
        self.assertEqual(DailyPlanItem.objects.count(), 1)

    def test_raises_when_existing_and_replace_false(self):
        tasks = [self._make_task()]
        available_times = [self._fake_available_time(date(2026, 8, 1), 60)]

        generate_schedule(
            exam_period=self.exam_period,
            study_tasks=tasks,
            available_times=available_times,
        )

        with self.assertRaises(ScheduleAlreadyExistsError):
            generate_schedule(
                exam_period=self.exam_period,
                study_tasks=tasks,
                available_times=available_times,
                replace_existing=False,
            )
        self.assertEqual(DailyPlanItem.objects.count(), 1)

    def test_replace_existing_true_clears_old_items(self):
        task_v1 = self._make_task(importance="high", order=1)
        available_times = [self._fake_available_time(date(2026, 8, 1), 60)]

        generate_schedule(
            exam_period=self.exam_period,
            study_tasks=[task_v1],
            available_times=available_times,
        )

        task_v2 = self._make_task(importance="low", order=2)
        generate_schedule(
            exam_period=self.exam_period,
            study_tasks=[task_v2],
            available_times=available_times,
            replace_existing=True,
        )

        items = DailyPlanItem.objects.all()
        self.assertEqual(items.count(), 1)
        self.assertEqual(items.first().study_task_id, task_v2.id)

    def test_replace_existing_blocked_when_progress_log_exists(self):
        from core.choices import ProgressStatus

        task_v1 = self._make_task(importance="high", order=1)
        available_times = [self._fake_available_time(date(2026, 8, 1), 60)]

        generate_schedule(
            exam_period=self.exam_period,
            study_tasks=[task_v1],
            available_times=available_times,
        )

        recorded_item = DailyPlanItem.objects.get(study_task=task_v1)
        progress_log = ProgressLog.objects.create(
            daily_plan_item=recorded_item,
            progress_status=ProgressStatus.DONE,
            actual_minutes=60,
            completion_percent=100,
        )
        progress_log_id = progress_log.id

        task_v2 = self._make_task(importance="low", order=2)
        with self.assertRaises(ScheduleHasProgressError):
            generate_schedule(
                exam_period=self.exam_period,
                study_tasks=[task_v2],
                available_times=available_times,
                replace_existing=True,
            )

        # 기존 DailyPlanItem이 그대로 남아있어야 한다.
        self.assertEqual(DailyPlanItem.objects.count(), 1)
        self.assertEqual(DailyPlanItem.objects.first().study_task_id, task_v1.id)
        # ProgressLog는 개수뿐 아니라 원래 기록 내용까지 그대로 보존돼야 한다.
        self.assertTrue(
            ProgressLog.objects.filter(
                id=progress_log_id,
                daily_plan_item=recorded_item,
                actual_minutes=60,
                completion_percent=100,
            ).exists()
        )

    def test_replace_existing_still_works_when_no_progress_recorded(self):
        task_v1 = self._make_task(importance="high", order=1)
        available_times = [self._fake_available_time(date(2026, 8, 1), 60)]

        generate_schedule(
            exam_period=self.exam_period,
            study_tasks=[task_v1],
            available_times=available_times,
        )

        task_v2 = self._make_task(importance="low", order=2)
        result = generate_schedule(
            exam_period=self.exam_period,
            study_tasks=[task_v2],
            available_times=available_times,
            replace_existing=True,
        )
        self.assertEqual(result["created_item_count"], 1)
        self.assertEqual(DailyPlanItem.objects.count(), 1)
        self.assertEqual(DailyPlanItem.objects.first().study_task_id, task_v2.id)

    def test_no_data_saved_when_unallocated_tasks_exist(self):
        task = self._make_task(estimated_max_minutes=100)
        available_times = [self._fake_available_time(date(2026, 8, 1), 60)]

        with self.assertRaises(UnallocatedTasksError):
            generate_schedule(
                exam_period=self.exam_period,
                study_tasks=[task],
                available_times=available_times,
            )

        self.assertEqual(DailyPlan.objects.filter(exam_period=self.exam_period).count(), 0)
        self.assertEqual(DailyPlanItem.objects.count(), 0)

    def test_same_date_tasks_share_one_daily_plan(self):
        task1 = self._make_task(order=1, estimated_max_minutes=30)
        task2 = self._make_task(order=2, estimated_max_minutes=30)
        available_times = [self._fake_available_time(date(2026, 8, 1), 60)]

        generate_schedule(
            exam_period=self.exam_period,
            study_tasks=[task1, task2],
            available_times=available_times,
        )
    def test_raises_when_task_belongs_to_other_exam_period(self):
        from planner.services.schedule_generator import MismatchedExamPeriodError
        from exams.models import Exam, ExamPeriod

        other_exam_period = ExamPeriod.objects.create(
            user=self.user,
            title="다른 시험기간",
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 20),
        )
        other_exam = Exam.objects.create(
            exam_period=other_exam_period,
            subject_name="다른 과목",
            exam_date=date(2026, 9, 10),
        )
        other_task = self._make_task(exam=other_exam)
        available_times = [self._fake_available_time(date(2026, 8, 1), 60)]

        with self.assertRaises(MismatchedExamPeriodError):
            generate_schedule(
                exam_period=self.exam_period,
                study_tasks=[other_task],
                available_times=available_times,
            )

class FinalizeDailyPlanTests(TestCase):
    """
    finalize_daily_plan()과 recovery.py 연동 테스트.
    공통 셋업: 사용자 1명, 시험기간(오늘~오늘+10일), DailyPlan은 오늘 날짜로 생성.
    """

    def setUp(self):
        from django.contrib.auth import get_user_model
        from exams.models import Exam, ExamPeriod, StudyTask

        User = get_user_model()
        self.user = User.objects.create_user(
            username="finalize_tester", email="finalize@example.com", password="pass1234"
        )
        self.today = django_timezone.localdate()
        self.exam_period = ExamPeriod.objects.create(
            user=self.user,
            title="복구 테스트 시험기간",
            start_date=self.today - timedelta(days=1),
            end_date=self.today + timedelta(days=10),
        )

    def _make_exam(self, exam_date):
        from exams.models import Exam
        return Exam.objects.create(
            exam_period=self.exam_period,
            subject_name="테스트 과목",
            exam_date=exam_date,
        )

    def _make_task(self, exam, importance="high", depth="basic",
                    task_type="concept", difficulty="normal", order=1):
        from exams.models import StudyTask
        return StudyTask.objects.create(
            exam=exam,
            title="복구 대상 작업",
            importance=importance,
            depth=depth,
            task_type=task_type,
            difficulty=difficulty,
            order=order,
            estimated_min_minutes=20,
            estimated_max_minutes=40,
            is_confirmed=True,
        )

    def _make_daily_plan(self, date, available_minutes=60, planned_minutes=40):
        return DailyPlan.objects.create(
            exam_period=self.exam_period,
            date=date,
            available_minutes=available_minutes,
            planned_minutes=planned_minutes,
        )

    def _make_item(self, daily_plan, task, planned_minutes=40, order=1):
        return DailyPlanItem.objects.create(
            daily_plan=daily_plan,
            study_task=task,
            planned_minutes=planned_minutes,
            order=order,
        )

    def _record(self, item, status, actual_minutes=None, completion_percent=None):
        record_progress(
            daily_plan_item=item,
            status=status,
            actual_minutes=actual_minutes,
            completion_percent=completion_percent,
        )

    # ── 1. 분량 유지형 성공 ──────────────────────────────
    def test_maintain_volume_succeeds_when_capacity_enough(self):
        exam = self._make_exam(exam_date=self.today + timedelta(days=5))
        task = self._make_task(exam, importance="high", depth="core")
        daily_plan = self._make_daily_plan(self.today)
        item = self._make_item(daily_plan, task)
        self._record(item, "not_done")

        AvailableTime.objects.create(
            exam_period=self.exam_period,
            date=self.today + timedelta(days=1),
            available_minutes=40,
        )

        result = finalize_daily_plan(daily_plan)

        self.assertTrue(result["needs_recovery"])
        recovery = result["recovery_plans"]
        self.assertIsNotNone(recovery["maintain_volume"])
        self.assertEqual(
            recovery["maintain_volume"].items.count(), 1
        )

    # ── 2. 분량 유지형 실패 시 Plan 미생성 ──────────────
    def test_maintain_volume_fails_when_capacity_insufficient(self):
        exam = self._make_exam(exam_date=self.today + timedelta(days=5))
        task = self._make_task(exam, importance="high", depth="core")
        daily_plan = self._make_daily_plan(self.today)
        item = self._make_item(daily_plan, task)
        self._record(item, "not_done")
        # 미래 가용시간을 아예 만들지 않음 -> 배치 불가

        result = finalize_daily_plan(daily_plan)

        recovery = result["recovery_plans"]
        self.assertIsNone(recovery["maintain_volume"])
        self.assertIsNotNone(recovery["maintain_volume_failure_reason"])
        self.assertFalse(
            RecoveryPlan.objects.filter(recovery_type=RecoveryType.MAINTAIN_VOLUME).exists()
        )

    # ── 3. 핵심 집중형에서 실제 제외 작업 1개 이상 생성 (보호 작업은 유지) ──
    def test_core_focus_always_excludes_at_least_one_when_candidate_exists(self):
        exam = self._make_exam(exam_date=self.today + timedelta(days=5))
        low_task = self._make_task(exam, importance="low", depth="optional", order=1)
        protected_task = self._make_task(exam, importance="high", depth="core", order=2)
        daily_plan = self._make_daily_plan(self.today)
        low_item = self._make_item(daily_plan, low_task, order=1)
        protected_item = self._make_item(daily_plan, protected_task, order=2)
        self._record(low_item, "not_done")
        self._record(protected_item, "not_done")

        # 두 작업(각 40분) 다 배치 가능한 넉넉한 용량 -> 분량유지형도 성공
        AvailableTime.objects.create(
            exam_period=self.exam_period,
            date=self.today + timedelta(days=1),
            available_minutes=80,
        )

        result = finalize_daily_plan(daily_plan)
        recovery = result["recovery_plans"]

        self.assertIsNotNone(recovery["maintain_volume"])
        self.assertIsNotNone(recovery["core_focus"])

        excluded_task_ids = set(
            recovery["core_focus"].items.filter(
                action_type="exclude"
            ).values_list("study_task_id", flat=True)
        )
        self.assertGreaterEqual(len(excluded_task_ids), 1)
        self.assertIn(low_task.id, excluded_task_ids)
        self.assertNotIn(protected_task.id, excluded_task_ids)
        self.assertEqual(
            recovery["maintain_volume"].recovery_group_id,
            recovery["core_focus"].recovery_group_id,
        )

    # ── 4. 가까운 시험 작업이 보호되는지 확인 ────────────
    def test_core_focus_protects_near_exam_excludes_far_exam_first(self):
        near_exam = self._make_exam(exam_date=self.today + timedelta(days=3))
        far_exam = self._make_exam(exam_date=self.today + timedelta(days=8))

        near_task = self._make_task(
            near_exam, importance="low", depth="optional", order=1
        )
        far_task = self._make_task(
            far_exam, importance="low", depth="optional", order=2
        )

        daily_plan = self._make_daily_plan(self.today)
        near_item = self._make_item(daily_plan, near_task, order=1)
        far_item = self._make_item(daily_plan, far_task, order=2)
        self._record(near_item, "not_done")
        self._record(far_item, "not_done")

        # 두 작업(각 40분) 다 배치하기엔 부족하지만, 하나만 빼면 충분한 용량
        AvailableTime.objects.create(
            exam_period=self.exam_period,
            date=self.today + timedelta(days=1),
            available_minutes=40,
        )

        result = finalize_daily_plan(daily_plan)
        core_focus = result["recovery_plans"]["core_focus"]

        self.assertIsNotNone(core_focus)
        excluded_tasks = set(
            core_focus.items.filter(action_type="exclude").values_list(
                "study_task_id", flat=True
            )
        )
        rescheduled_tasks = set(
            core_focus.items.filter(action_type="reschedule").values_list(
                "study_task_id", flat=True
            )
        )
        # 시험일이 먼 작업(far_task)이 제외되고, 가까운 작업(near_task)은 재배치돼야 함
        self.assertIn(far_task.id, excluded_tasks)
        self.assertIn(near_task.id, rescheduled_tasks)

    # ── 5. 순차 중복 호출 시 복구 그룹이 추가 생성되지 않는지 확인 ───────
    # (실제 동시 요청 레이스 컨디션은 여기서 검증하지 않음. MVP에서는
    #  finalize_daily_plan() 내부 조건부 UPDATE로 방어하며, 실제 동시성
    #  검증은 TransactionTestCase + 별도 스레드/PostgreSQL 환경이 필요함)
    def test_finalize_twice_raises_and_does_not_duplicate_recovery(self):
        exam = self._make_exam(exam_date=self.today + timedelta(days=5))
        task = self._make_task(exam, importance="high", depth="core")
        daily_plan = self._make_daily_plan(self.today)
        item = self._make_item(daily_plan, task)
        self._record(item, "not_done")

        AvailableTime.objects.create(
            exam_period=self.exam_period,
            date=self.today + timedelta(days=1),
            available_minutes=40,
        )

        finalize_daily_plan(daily_plan)
        recovery_group_count_after_first = RecoveryPlan.objects.values(
            "recovery_group_id"
        ).distinct().count()

        daily_plan.refresh_from_db()
        with self.assertRaises(DailyPlanAlreadyFinalizedError):
            finalize_daily_plan(daily_plan)

        recovery_group_count_after_second = RecoveryPlan.objects.values(
            "recovery_group_id"
        ).distinct().count()

        self.assertEqual(
            recovery_group_count_after_first, recovery_group_count_after_second
        )

    # ── 6. 마감 후 진행 기록 수정 거부 ────────────────────
    def test_record_progress_rejected_after_finalize(self):
        exam = self._make_exam(exam_date=self.today + timedelta(days=5))
        task = self._make_task(exam, importance="high", depth="core")
        daily_plan = self._make_daily_plan(self.today)
        item = self._make_item(daily_plan, task)
        self._record(item, "done", actual_minutes=30)

        finalize_daily_plan(daily_plan)

        with self.assertRaises(FinalizedDailyPlanEditError):
            record_progress(
                daily_plan_item=item,
                status="done",
                actual_minutes=35,
            )

    # ── 7. 미래 계획은 마감 거부 (과거 계획은 허용) ───────
    def test_finalize_rejects_future_daily_plan(self):
        exam = self._make_exam(exam_date=self.today + timedelta(days=5))
        task = self._make_task(exam, importance="high", depth="core")
        future_plan = self._make_daily_plan(self.today + timedelta(days=1))
        item = self._make_item(future_plan, task)
        self._record(item, "done", actual_minutes=30)

        with self.assertRaises(FutureDailyPlanFinalizeError):
            finalize_daily_plan(future_plan)

    def test_finalize_allows_past_daily_plan(self):
        exam = self._make_exam(exam_date=self.today + timedelta(days=5))
        task = self._make_task(exam, importance="high", depth="core")
        past_plan = self._make_daily_plan(self.today - timedelta(days=1))
        item = self._make_item(past_plan, task)
        self._record(item, "not_done")

        AvailableTime.objects.create(
            exam_period=self.exam_period,
            date=self.today + timedelta(days=1),
            available_minutes=40,
        )

        result = finalize_daily_plan(past_plan)

        self.assertTrue(result["needs_recovery"])
        # 복구 배치는 반드시 오늘 이후(내일부터)여야 한다
        for item in result["recovery_plans"]["maintain_volume"].items.all():
            if item.changed_date is not None:
                self.assertGreater(item.changed_date, self.today)

    # ── 8. (보너스) 완료 항목만 있으면 복구 없음 ─────────
    def test_finalize_no_recovery_when_all_completed(self):
        exam = self._make_exam(exam_date=self.today + timedelta(days=5))
        task = self._make_task(exam, importance="high", depth="core")
        daily_plan = self._make_daily_plan(self.today)
        item = self._make_item(daily_plan, task)
        self._record(item, "done", actual_minutes=30)

        result = finalize_daily_plan(daily_plan)

        self.assertFalse(result["needs_recovery"])
        self.assertIsNone(result["recovery_plans"])

    # ── 9. 진행 기록 누락 시 finalized_at이 롤백되는지 확인 ─────
    def test_finalize_with_unrecorded_item_rolls_back_claim(self):
        exam = self._make_exam(exam_date=self.today + timedelta(days=5))
        task = self._make_task(exam, importance="high", depth="core")
        daily_plan = self._make_daily_plan(self.today)
        self._make_item(daily_plan, task)
        # ProgressLog를 일부러 만들지 않는다.

        with self.assertRaises(IncompleteProgressError):
            finalize_daily_plan(daily_plan)

        daily_plan.refresh_from_db()

        self.assertIsNone(daily_plan.finalized_at)
        self.assertFalse(
            RecoveryPlan.objects.filter(exam_period=self.exam_period).exists()
        )

    # ── 10. PARTIAL 항목의 남은 시간이 estimated_max × 잔여비율로 계산되는지 ──
    def test_partial_item_uses_latest_speed_factor_and_remaining_ratio(self):
        exam = self._make_exam(exam_date=self.today + timedelta(days=5))
        task = self._make_task(
            exam, importance="high", depth="core",
            task_type="concept", difficulty="normal",
        )
        daily_plan = self._make_daily_plan(self.today)
        item = self._make_item(daily_plan, task)

        self._record(item, "partial", actual_minutes=30, completion_percent=50)

        exam.refresh_from_db()
        _estimated_min, estimated_max = estimate_task_minutes(
            task.task_type, task.difficulty, exam.speed_factor
        )
        expected_remaining = round_up_to_five(estimated_max * 0.5)

        AvailableTime.objects.create(
            exam_period=self.exam_period,
            date=self.today + timedelta(days=1),
            available_minutes=200,
        )

        result = finalize_daily_plan(daily_plan)

        recovery_item = (
            result["recovery_plans"]["maintain_volume"].items.get(study_task=task)
        )
        self.assertEqual(recovery_item.remaining_minutes, expected_remaining)

    # ── 11. NOT_DONE에 시간이 들어와도 0으로 고정되는지 (record_progress 저장까지) ──
    def test_not_done_progress_stores_zero_minutes(self):
        exam = self._make_exam(exam_date=self.today + timedelta(days=5))
        task = self._make_task(exam, importance="high", depth="core")
        daily_plan = self._make_daily_plan(self.today)
        item = self._make_item(daily_plan, task)

        result = record_progress(
            daily_plan_item=item, status="not_done", actual_minutes=50,
        )
        self.assertEqual(result["progress_log"].actual_minutes, 0)

    # ── 12. 미래 일정에 이미 배치된 작업이 복구 가용시간에서 차감되는지 ──
    def test_existing_future_items_reduce_recovery_capacity(self):
        exam = self._make_exam(exam_date=self.today + timedelta(days=5))

        unfinished_task = self._make_task(exam, importance="high", depth="core", order=1)
        current_plan = self._make_daily_plan(self.today)
        unfinished_item = self._make_item(current_plan, unfinished_task)
        self._record(unfinished_item, "not_done")

        tomorrow = self.today + timedelta(days=1)
        AvailableTime.objects.create(
            exam_period=self.exam_period, date=tomorrow, available_minutes=60,
        )

        occupied_task = self._make_task(exam, importance="high", depth="core", order=2)
        future_plan = self._make_daily_plan(tomorrow, available_minutes=60, planned_minutes=30)
        self._make_item(future_plan, occupied_task, planned_minutes=30)

        result = finalize_daily_plan(current_plan)

        # 가용시간 60분 - 기존 작업 30분 = 30분만 남으므로,
        # estimated_max 기준 40분짜리 미완료 작업은 들어갈 자리가 없어야 함
        self.assertIsNone(result["recovery_plans"]["maintain_volume"])

    # ── 13. 핵심 집중형이 유일한 작업까지 전부 제외하지 않는지 ──
    def test_core_focus_does_not_exclude_every_task(self):
        exam = self._make_exam(exam_date=self.today + timedelta(days=5))
        task = self._make_task(exam, importance="low", depth="optional")
        daily_plan = self._make_daily_plan(self.today)
        item = self._make_item(daily_plan, task)
        self._record(item, "not_done")

        AvailableTime.objects.create(
            exam_period=self.exam_period,
            date=self.today + timedelta(days=1),
            available_minutes=40,
        )

        result = finalize_daily_plan(daily_plan)
        recovery = result["recovery_plans"]

        self.assertIsNotNone(recovery["maintain_volume"])
        self.assertIsNone(recovery["core_focus"])
        self.assertIsNotNone(recovery["core_focus_failure_reason"])

    # ── 14. 전체 완료 시 DB에도 복구안이 없고 finalized_at은 저장됨 ──
    def test_finalize_no_recovery_when_all_completed_db_check(self):
        exam = self._make_exam(exam_date=self.today + timedelta(days=5))
        task = self._make_task(exam, importance="high", depth="core")
        daily_plan = self._make_daily_plan(self.today)
        item = self._make_item(daily_plan, task)
        self._record(item, "done", actual_minutes=30)

        finalize_daily_plan(daily_plan)
        daily_plan.refresh_from_db()

        self.assertIsNotNone(daily_plan.finalized_at)
        self.assertFalse(
            RecoveryPlan.objects.filter(exam_period=self.exam_period).exists()
        )

# ── 15. RecoveryPlan에 source_daily_plan이 정확히 저장되는지 ──────
    def test_recovery_plans_set_source_daily_plan(self):
        exam = self._make_exam(exam_date=self.today + timedelta(days=5))
        task = self._make_task(exam, importance="high", depth="core")
        daily_plan = self._make_daily_plan(self.today)
        item = self._make_item(daily_plan, task)
        self._record(item, "not_done")

        AvailableTime.objects.create(
            exam_period=self.exam_period,
            date=self.today + timedelta(days=1),
            available_minutes=40,
        )

        result = finalize_daily_plan(daily_plan)
        recovery = result["recovery_plans"]

        self.assertEqual(recovery["maintain_volume"].source_daily_plan, daily_plan)

    # ── 16. RecoveryPlan.exam_period가 source_daily_plan.exam_period와 항상 일치 ──
    def test_recovery_plan_exam_period_matches_source_daily_plan(self):
        exam = self._make_exam(exam_date=self.today + timedelta(days=5))
        task = self._make_task(exam, importance="high", depth="core")
        daily_plan = self._make_daily_plan(self.today)
        item = self._make_item(daily_plan, task)
        self._record(item, "not_done")

        AvailableTime.objects.create(
            exam_period=self.exam_period,
            date=self.today + timedelta(days=1),
            available_minutes=40,
        )

        finalize_daily_plan(daily_plan)

        plans = RecoveryPlan.objects.filter(source_daily_plan=daily_plan)
        self.assertTrue(plans.exists())
        for plan in plans:
            self.assertEqual(plan.exam_period_id, daily_plan.exam_period_id)

    # ── 17. 옵션 False면 기존처럼 IncompleteProgressError ──────
    def test_finalize_without_auto_mark_still_raises_on_unrecorded(self):
        exam = self._make_exam(exam_date=self.today + timedelta(days=5))
        task = self._make_task(exam, importance="high", depth="core")
        daily_plan = self._make_daily_plan(self.today)
        self._make_item(daily_plan, task)

        with self.assertRaises(IncompleteProgressError):
            finalize_daily_plan(daily_plan, mark_unrecorded_as_not_done=False)

    # ── 18. 옵션 True면 미입력 항목이 전부 NOT_DONE으로 기록됨 ──
    def test_finalize_auto_marks_unrecorded_as_not_done(self):
        exam = self._make_exam(exam_date=self.today + timedelta(days=5))
        task = self._make_task(exam, importance="high", depth="core")
        daily_plan = self._make_daily_plan(self.today)
        item = self._make_item(daily_plan, task)

        result = finalize_daily_plan(daily_plan, mark_unrecorded_as_not_done=True)

        item.refresh_from_db()
        self.assertEqual(item.progress_log.progress_status, "not_done")
        self.assertEqual(item.progress_log.actual_minutes, 0)
        self.assertEqual(item.progress_log.completion_percent, 0)
        self.assertEqual(result["auto_marked_not_done_count"], 1)

    # ── 19. 자동 기록 후 finalized_at 정상 저장 ────────────────
    def test_finalize_auto_mark_still_saves_finalized_at(self):
        exam = self._make_exam(exam_date=self.today + timedelta(days=5))
        task = self._make_task(exam, importance="high", depth="core")
        daily_plan = self._make_daily_plan(self.today)
        self._make_item(daily_plan, task)

        finalize_daily_plan(daily_plan, mark_unrecorded_as_not_done=True)

        daily_plan.refresh_from_db()
        self.assertIsNotNone(daily_plan.finalized_at)

    # ── 20. 자동 기록된 항목이 복구 대상에 포함됨 ──────────────
    def test_auto_marked_items_included_in_recovery(self):
        exam = self._make_exam(exam_date=self.today + timedelta(days=5))
        task = self._make_task(exam, importance="high", depth="core")
        daily_plan = self._make_daily_plan(self.today)
        item = self._make_item(daily_plan, task)

        AvailableTime.objects.create(
            exam_period=self.exam_period,
            date=self.today + timedelta(days=1),
            available_minutes=60,
        )

        result = finalize_daily_plan(daily_plan, mark_unrecorded_as_not_done=True)

        self.assertTrue(result["needs_recovery"])
        self.assertIn(item, result["unfinished_items"])
        self.assertIsNotNone(result["recovery_plans"])

    # ── 21. 자동 기록 경로에서도 속도 재계산을 별도로 수행하지 않음 ──
    # (기존 CalculateSpeedFactorTests.test_not_done_logs_excluded가
    #  "NOT_DONE 로그 자체가 계산에서 제외됨"을 검증한다면,
    #  이 테스트는 "자동 마감 경로를 타도 speed_factor가 아예 바뀌지 않는다"를 검증)
    def test_auto_marked_not_done_does_not_affect_speed_factor(self):
        exam = self._make_exam(exam_date=self.today + timedelta(days=5))
        task = self._make_task(exam, importance="high", depth="core")
        daily_plan = self._make_daily_plan(self.today)
        self._make_item(daily_plan, task)

        finalize_daily_plan(daily_plan, mark_unrecorded_as_not_done=True)

        exam.refresh_from_db()
        self.assertEqual(exam.speed_factor, 1.0)

    # ── 22. 마감 처리 중 예외 발생 시 자동 생성 로그와 선점도 함께 롤백 ──
    @patch(
        "planner.services.progress_recorder.generate_recovery_options",
        side_effect=RuntimeError("복구안 생성 실패"),
    )
    def test_finalize_rolls_back_auto_marked_logs_on_error(self, _mock):
        exam = self._make_exam(exam_date=self.today + timedelta(days=5))
        task = self._make_task(exam, importance="high", depth="core")
        daily_plan = self._make_daily_plan(self.today)
        item = self._make_item(daily_plan, task)

        with self.assertRaises(RuntimeError):
            finalize_daily_plan(daily_plan, mark_unrecorded_as_not_done=True)

        self.assertFalse(
            ProgressLog.objects.filter(daily_plan_item=item).exists()
        )
        daily_plan.refresh_from_db()
        self.assertIsNone(daily_plan.finalized_at)

    def test_recovery_plan_creation_validates_exam_period_consistency(self):
        from django.core.exceptions import ValidationError
        from unittest.mock import patch
        from exams.models import ExamPeriod

        exam = self._make_exam(exam_date=self.today + timedelta(days=5))
        task = self._make_task(exam, importance="high", depth="core")
        daily_plan = self._make_daily_plan(self.today)
        item = self._make_item(daily_plan, task)
        self._record(item, "not_done")

        AvailableTime.objects.create(
            exam_period=self.exam_period,
            date=self.today + timedelta(days=1),
            available_minutes=40,
        )

        # source_daily_plan.exam_period와 다른 exam_period를 강제로 넣도록 create를 패치
        other_exam_period = ExamPeriod.objects.create(
            user=self.user,
            title="다른 시험기간",
            start_date=self.today,
            end_date=self.today + timedelta(days=10),
        )

        original_create = RecoveryPlan.objects.create
        def broken_create(*args, **kwargs):
            kwargs['exam_period'] = other_exam_period
            return original_create(*args, **kwargs)

        with patch.object(RecoveryPlan.objects, 'create', side_effect=broken_create):
            with self.assertRaises(ValidationError):
                finalize_daily_plan(daily_plan)


class ApplyRecoveryPlanTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        from exams.models import Exam, ExamPeriod, StudyTask

        User = get_user_model()
        self.user = User.objects.create_user(
            username="apply_tester", email="apply@example.com", password="pass1234"
        )
        self.today = django_timezone.localdate()
        self.exam_period = ExamPeriod.objects.create(
            user=self.user,
            title="복구 적용 테스트 시험기간",
            start_date=self.today - timedelta(days=1),
            end_date=self.today + timedelta(days=10),
        )
        self.exam = Exam.objects.create(
            exam_period=self.exam_period,
            subject_name="테스트 과목",
            exam_date=self.today + timedelta(days=5),
        )
        self.protected_task = StudyTask.objects.create(
            exam=self.exam, title="보호 작업", importance="high", depth="core",
            task_type="concept", difficulty="normal", order=1,
            estimated_min_minutes=20, estimated_max_minutes=40, is_confirmed=True,
        )
        self.low_task = StudyTask.objects.create(
            exam=self.exam, title="제외 후보 작업", importance="low", depth="optional",
            task_type="concept", difficulty="normal", order=2,
            estimated_min_minutes=20, estimated_max_minutes=40, is_confirmed=True,
        )
        self.daily_plan = DailyPlan.objects.create(
            exam_period=self.exam_period, date=self.today,
            available_minutes=80, planned_minutes=80,
        )
        self.protected_item = DailyPlanItem.objects.create(
            daily_plan=self.daily_plan, study_task=self.protected_task,
            planned_minutes=40, order=1,
        )
        self.low_item = DailyPlanItem.objects.create(
            daily_plan=self.daily_plan, study_task=self.low_task,
            planned_minutes=40, order=2,
        )
        record_progress(daily_plan_item=self.protected_item, status="not_done", actual_minutes=0)
        record_progress(daily_plan_item=self.low_item, status="not_done", actual_minutes=0)

        AvailableTime.objects.create(
            exam_period=self.exam_period,
            date=self.today + timedelta(days=1),
            available_minutes=80,
        )

        result = finalize_daily_plan(self.daily_plan)
        self.maintain_volume = result["recovery_plans"]["maintain_volume"]
        self.core_focus = result["recovery_plans"]["core_focus"]
        self.assertIsNotNone(self.maintain_volume)
        self.assertIsNotNone(self.core_focus)

    def test_apply_creates_daily_plan_item_on_changed_date(self):
        reschedule_item = self.maintain_volume.items.get(
            study_task=self.protected_task, action_type="reschedule"
        )

        apply_recovery_plan(self.maintain_volume)

        new_daily_plan = DailyPlan.objects.get(
            exam_period=self.exam_period, date=reschedule_item.changed_date
        )
        new_item = new_daily_plan.items.get(study_task=self.protected_task)
        self.assertEqual(new_item.planned_minutes, reschedule_item.remaining_minutes)

    def test_apply_does_not_create_item_for_excluded_task(self):
        apply_recovery_plan(self.core_focus)

        excluded_task_ids = set(
            self.core_focus.items.filter(action_type="exclude")
            .values_list("study_task_id", flat=True)
        )
        self.assertIn(self.low_task.id, excluded_task_ids)
        self.assertFalse(
            DailyPlanItem.objects.filter(
                study_task=self.low_task, daily_plan__date__gt=self.today
            ).exists()
        )

    def test_apply_marks_plan_applied_and_sibling_discarded(self):
        from core.choices import RecoveryPlanStatus

        apply_recovery_plan(self.maintain_volume)

        self.maintain_volume.refresh_from_db()
        self.core_focus.refresh_from_db()

        self.assertEqual(self.maintain_volume.status, RecoveryPlanStatus.APPLIED)
        self.assertIsNotNone(self.maintain_volume.applied_at)
        self.assertEqual(self.core_focus.status, RecoveryPlanStatus.DISCARDED)

    def test_apply_twice_raises(self):
        apply_recovery_plan(self.maintain_volume)

        with self.assertRaises(RecoveryPlanAlreadyProcessedError):
            apply_recovery_plan(self.maintain_volume)

    def test_apply_sibling_after_one_applied_raises(self):
        apply_recovery_plan(self.maintain_volume)

        with self.assertRaises(RecoveryPlanAlreadyProcessedError):
            apply_recovery_plan(self.core_focus)

    def test_apply_discarded_plan_raises(self):
        from core.choices import RecoveryPlanStatus

        self.maintain_volume.status = RecoveryPlanStatus.DISCARDED
        self.maintain_volume.save(update_fields=["status"])

        with self.assertRaises(RecoveryPlanAlreadyProcessedError):
            apply_recovery_plan(self.maintain_volume)

    def test_apply_raises_stale_when_available_time_deleted(self):
        AvailableTime.objects.filter(
            exam_period=self.exam_period, date=self.today + timedelta(days=1),
        ).delete()

        with self.assertRaises(RecoveryPlanStaleError):
            apply_recovery_plan(self.maintain_volume)

        self.maintain_volume.refresh_from_db()
        from core.choices import RecoveryPlanStatus
        self.assertEqual(self.maintain_volume.status, RecoveryPlanStatus.PENDING)

    def test_apply_raises_stale_when_capacity_reduced(self):
        available_time = AvailableTime.objects.get(
            exam_period=self.exam_period, date=self.today + timedelta(days=1),
        )
        available_time.available_minutes = 10
        available_time.save(update_fields=["available_minutes"])

        with self.assertRaises(RecoveryPlanStaleError):
            apply_recovery_plan(self.maintain_volume)

        self.assertFalse(
            DailyPlanItem.objects.filter(
                study_task__in=[self.protected_task, self.low_task],
                daily_plan__date__gt=self.today,
            ).exists()
        )

    def test_apply_rejects_past_changed_date(self):
        recovery_item = self.maintain_volume.items.filter(action_type="reschedule").first()
        recovery_item.changed_date = self.today
        recovery_item.save(update_fields=["changed_date"])

        AvailableTime.objects.get_or_create(
            exam_period=self.exam_period, date=self.today,
            defaults={"available_minutes": 80},
        )

        with self.assertRaises(RecoveryPlanStaleError):
            apply_recovery_plan(self.maintain_volume)

    def test_apply_updates_existing_daily_plan_minutes_and_order(self):
        tomorrow = self.today + timedelta(days=1)

        existing_task = type(self.protected_task).objects.create(
            exam=self.exam, title="기존 작업", importance="high", depth="core",
            task_type="concept", difficulty="normal", order=3,
            estimated_min_minutes=20, estimated_max_minutes=20, is_confirmed=True,
        )
        future_plan = DailyPlan.objects.create(
            exam_period=self.exam_period, date=tomorrow,
            available_minutes=100, planned_minutes=20,
        )
        DailyPlanItem.objects.create(
            daily_plan=future_plan, study_task=existing_task,
            planned_minutes=20, order=3,
        )

        AvailableTime.objects.filter(
            exam_period=self.exam_period, date=tomorrow,
        ).update(available_minutes=100)

        apply_recovery_plan(self.core_focus)

        future_plan.refresh_from_db()
        orders = list(future_plan.items.order_by("order").values_list("order", flat=True))

        self.assertEqual(
            future_plan.planned_minutes,
            sum(future_plan.items.values_list("planned_minutes", flat=True)),
        )
        self.assertEqual(orders, [3, 4])

    def test_apply_raises_invalid_data_when_remaining_minutes_not_positive(self):
        from planner.services.recovery import RecoveryPlanInvalidDataError

        recovery_item = self.maintain_volume.items.filter(action_type="reschedule").first()
        recovery_item.remaining_minutes = 0
        recovery_item.save(update_fields=["remaining_minutes"])

        with self.assertRaises(RecoveryPlanInvalidDataError):
            apply_recovery_plan(self.maintain_volume)

class PlanGenerateFlowTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        from exams.models import Exam, ExamPeriod, StudyTask

        User = get_user_model()
        self.user = User.objects.create_user(
            username="plan_gen_tester", email="plangen@example.com", password="pass1234"
        )
        self.other_user = User.objects.create_user(
            username="other_tester", email="other@example.com", password="pass1234"
        )
        self.today = django_timezone.localdate()
        self.exam_period = ExamPeriod.objects.create(
            user=self.user,
            title="계획 생성 테스트 시험기간",
            start_date=self.today,
            end_date=self.today + timedelta(days=10),
            status="active",
        )
        self.exam = Exam.objects.create(
            exam_period=self.exam_period,
            subject_name="테스트 과목",
            exam_date=self.today + timedelta(days=5),
        )
        self.client.login(username="plangen@example.com", password="pass1234")

    def _make_confirmed_task(self, exam=None, order=1, min_m=20, max_m=40):
        from exams.models import StudyTask
        return StudyTask.objects.create(
            exam=exam or self.exam, title=f"작업 {order}", importance="high", depth="core",
            task_type="concept", difficulty="normal", order=order,
            estimated_min_minutes=min_m, estimated_max_minutes=max_m, is_confirmed=True,
        )

    def test_feasibility_blocks_other_user(self):
        response = self.client.get(
            reverse('planner:feasibility', kwargs={'period_id': self.exam_period.id})
        )
        self.client.login(username="other@example.com", password="pass1234")
        response = self.client.get(
            reverse('planner:feasibility', kwargs={'period_id': self.exam_period.id})
        )
        self.assertEqual(response.status_code, 404)

    def test_plan_generate_rejects_no_confirmed_tasks(self):
        response = self.client.post(
            reverse('planner:plan_generate', kwargs={'period_id': self.exam_period.id})
        )
        self.assertRedirects(
            response, reverse('planner:feasibility', kwargs={'period_id': self.exam_period.id})
        )
        self.assertEqual(DailyPlan.objects.filter(exam_period=self.exam_period).count(), 0)

    def test_plan_generate_rejects_unconfirmed_task_present(self):
        from exams.models import StudyTask
        self._make_confirmed_task(order=1)
        StudyTask.objects.create(
            exam=self.exam, title="미확정 작업", importance="high", depth="core",
            task_type="concept", difficulty="normal", order=2,
            estimated_min_minutes=20, estimated_max_minutes=40, is_confirmed=False,
        )
        AvailableTime.objects.create(exam_period=self.exam_period, date=self.today, available_minutes=100)

        response = self.client.post(
            reverse('planner:plan_generate', kwargs={'period_id': self.exam_period.id})
        )
        self.assertRedirects(
            response, reverse('planner:feasibility', kwargs={'period_id': self.exam_period.id})
        )
        self.assertEqual(DailyPlan.objects.filter(exam_period=self.exam_period).count(), 0)

    def test_plan_generate_rejects_subject_without_confirmed_task(self):
        from exams.models import Exam
        self._make_confirmed_task(order=1)
        Exam.objects.create(
            exam_period=self.exam_period, subject_name="작업 없는 과목",
            exam_date=self.today + timedelta(days=6),
        )
        AvailableTime.objects.create(exam_period=self.exam_period, date=self.today, available_minutes=100)

        response = self.client.post(
            reverse('planner:plan_generate', kwargs={'period_id': self.exam_period.id})
        )
        self.assertRedirects(
            response, reverse('planner:feasibility', kwargs={'period_id': self.exam_period.id})
        )
        self.assertEqual(DailyPlan.objects.filter(exam_period=self.exam_period).count(), 0)

    def test_plan_generate_rejects_zero_estimated_time_task(self):
        from exams.models import StudyTask
        StudyTask.objects.create(
            exam=self.exam, title="예상시간 0", importance="high", depth="core",
            task_type="concept", difficulty="normal", order=1,
            estimated_min_minutes=0, estimated_max_minutes=0, is_confirmed=True,
        )
        AvailableTime.objects.create(exam_period=self.exam_period, date=self.today, available_minutes=100)

        response = self.client.post(
            reverse('planner:plan_generate', kwargs={'period_id': self.exam_period.id})
        )
        self.assertRedirects(
            response, reverse('planner:feasibility', kwargs={'period_id': self.exam_period.id})
        )
        self.assertEqual(DailyPlan.objects.filter(exam_period=self.exam_period).count(), 0)

    def test_plan_generate_ignores_past_available_time(self):
        self._make_confirmed_task(order=1, min_m=20, max_m=40)
        AvailableTime.objects.create(
            exam_period=self.exam_period, date=self.today - timedelta(days=1), available_minutes=100
        )
        response = self.client.post(
            reverse('planner:plan_generate', kwargs={'period_id': self.exam_period.id})
        )
        self.assertRedirects(
            response, reverse('planner:feasibility', kwargs={'period_id': self.exam_period.id})
        )
        self.assertEqual(DailyPlan.objects.filter(exam_period=self.exam_period).count(), 0)

    def test_plan_generate_rejects_risky_feasibility(self):
        self._make_confirmed_task(order=1, min_m=100, max_m=200)
        AvailableTime.objects.create(exam_period=self.exam_period, date=self.today, available_minutes=150)

        response = self.client.post(
            reverse('planner:plan_generate', kwargs={'period_id': self.exam_period.id})
        )
        self.assertRedirects(
            response, reverse('planner:feasibility', kwargs={'period_id': self.exam_period.id})
        )
        self.assertEqual(DailyPlan.objects.filter(exam_period=self.exam_period).count(), 0)

    def test_plan_generate_success_allocates_all_tasks(self):
        self._make_confirmed_task(order=1, min_m=20, max_m=40)
        self._make_confirmed_task(order=2, min_m=20, max_m=40)
        AvailableTime.objects.create(exam_period=self.exam_period, date=self.today, available_minutes=100)

        response = self.client.post(
            reverse('planner:plan_generate', kwargs={'period_id': self.exam_period.id})
        )
        self.assertRedirects(response, reverse('planner:dashboard'))
        self.assertEqual(
            DailyPlanItem.objects.filter(daily_plan__exam_period=self.exam_period).count(), 2
        )

    def test_plan_generate_twice_redirects_to_dashboard(self):
        self._make_confirmed_task(order=1, min_m=20, max_m=40)
        AvailableTime.objects.create(exam_period=self.exam_period, date=self.today, available_minutes=100)

        self.client.post(reverse('planner:plan_generate', kwargs={'period_id': self.exam_period.id}))
        response = self.client.post(
            reverse('planner:plan_generate', kwargs={'period_id': self.exam_period.id})
        )
        self.assertRedirects(response, reverse('planner:dashboard'))
        self.assertEqual(
            DailyPlanItem.objects.filter(daily_plan__exam_period=self.exam_period).count(), 1
        )

    def test_feasibility_includes_available_time_edit_url(self):
        response = self.client.get(
            reverse('planner:feasibility', kwargs={'period_id': self.exam_period.id})
        )
        expected = reverse(
            'exams:available_time_update', kwargs={'period_id': self.exam_period.id}
        )
        self.assertEqual(response.context['available_time_edit_url'], expected)

    def test_subject_result_status_reflects_exam_date_order(self):
        """
        시험일이 빠른 과목이 가용시간을 먼저 차지하고, 뒤 과목은 남은 시간
        기준으로 판정돼야 한다.
        """
        from exams.models import Exam, StudyTask

        near_exam = Exam.objects.create(
            exam_period=self.exam_period, subject_name="임박 과목",
            exam_date=self.today + timedelta(days=2),
        )
        StudyTask.objects.create(
            exam=near_exam, title="임박 작업", importance="high", depth="core",
            task_type="concept", difficulty="normal", order=1,
            estimated_min_minutes=50, estimated_max_minutes=50, is_confirmed=True,
        )
        far_exam = Exam.objects.create(
            exam_period=self.exam_period, subject_name="여유 과목",
            exam_date=self.today + timedelta(days=8),
        )
        StudyTask.objects.create(
            exam=far_exam, title="여유 작업", importance="high", depth="core",
            task_type="concept", difficulty="normal", order=2,
            estimated_min_minutes=10, estimated_max_minutes=10, is_confirmed=True,
        )
        # 임박 과목 시험일(오늘+2) 전까지는 하루치(오늘)만 있고 가용시간이 부족함
        AvailableTime.objects.create(
            exam_period=self.exam_period, date=self.today, available_minutes=10,
        )
        # 시험일 이후엔 넉넉하지만 임박 과목엔 못 씀
        AvailableTime.objects.create(
            exam_period=self.exam_period, date=self.today + timedelta(days=5),
            available_minutes=100,
        )

        response = self.client.get(
            reverse('planner:feasibility', kwargs={'period_id': self.exam_period.id})
        )
        results = {s['subject_name']: s for s in response.context['subject_results']}

        # 임박 과목: 시험일 전 가용시간 10분 < 필요 50분 -> impossible
        self.assertEqual(results['임박 과목']['status'], IMPOSSIBLE)
        # 여유 과목: 남은 가용시간 넉넉함 -> possible
        self.assertEqual(results['여유 과목']['status'], POSSIBLE)

    def test_plan_generate_blocked_while_material_processing(self):
        from exams.models import StudyMaterial
        from core.choices import MaterialStatus

        StudyMaterial.objects.create(
            exam=self.exam, material_type='text',
            status=MaterialStatus.PROCESSING,
        )

        response = self.client.post(
            reverse('planner:plan_generate', kwargs={'period_id': self.exam_period.id})
        )

        self.assertRedirects(
            response, reverse('planner:feasibility', kwargs={'period_id': self.exam_period.id})
        )
        self.assertFalse(
            DailyPlan.objects.filter(exam_period=self.exam_period).exists()
        )

        
class FeasibilitySubjectResultsTests(TestCase):
    """
    #90 feasibility() subject_results context 테스트.
    """

    def setUp(self):
        from django.contrib.auth import get_user_model
        from exams.models import Exam, ExamPeriod, StudyTask

        User = get_user_model()
        self.user = User.objects.create_user(
            username="subject_results_tester",
            email="subject_results@example.com",
            password="pass1234",
        )
        self.other_user = User.objects.create_user(
            username="subject_results_other",
            email="subject_results_other@example.com",
            password="pass1234",
        )
        self.today = django_timezone.localdate()
        self.exam_period = ExamPeriod.objects.create(
            user=self.user,
            title="과목별 데이터 테스트 시험기간",
            start_date=self.today,
            end_date=self.today + timedelta(days=10),
            status="active",
        )
        self.client.login(username="subject_results@example.com", password="pass1234")

    def _get(self):
        return self.client.get(
            reverse("planner:feasibility", kwargs={"period_id": self.exam_period.id})
        )

    def _subject_results_by_name(self, response):
        return {s["subject_name"]: s for s in response.context["subject_results"]}

    # ── 1. 확정된 작업만 집계되는지 (미확정 제외) ──
    def test_only_confirmed_tasks_are_counted(self):
        from exams.models import Exam, StudyTask

        exam = Exam.objects.create(
            exam_period=self.exam_period, subject_name="데이터통신",
            exam_date=self.today + timedelta(days=5),
        )
        StudyTask.objects.create(
            exam=exam, title="확정 작업", importance="high", depth="core",
            task_type="concept", difficulty="normal", order=1,
            estimated_min_minutes=20, estimated_max_minutes=40, is_confirmed=True,
        )
        StudyTask.objects.create(
            exam=exam, title="미확정 작업", importance="high", depth="core",
            task_type="concept", difficulty="normal", order=2,
            estimated_min_minutes=30, estimated_max_minutes=50, is_confirmed=False,
        )

        response = self._get()
        results = self._subject_results_by_name(response)

        self.assertEqual(results["데이터통신"]["task_count"], 1)
        self.assertEqual(results["데이터통신"]["required_min_minutes"], 20)
        self.assertEqual(results["데이터통신"]["required_recommended_minutes"], 40)

    # ── 2. 작업이 아예 없는 과목도 0/0/0으로 나오는지 ──
    def test_subject_with_no_tasks_shows_zero(self):
        from exams.models import Exam

        Exam.objects.create(
            exam_period=self.exam_period, subject_name="운영체제",
            exam_date=self.today + timedelta(days=8),
        )

        response = self._get()
        results = self._subject_results_by_name(response)

        self.assertEqual(results["운영체제"]["task_count"], 0)
        self.assertEqual(results["운영체제"]["required_min_minutes"], 0)
        self.assertEqual(results["운영체제"]["required_recommended_minutes"], 0)

    # ── 3. exam_date 순으로 정렬되는지 ──
    def test_sorted_by_exam_date(self):
        from exams.models import Exam

        Exam.objects.create(
            exam_period=self.exam_period, subject_name="나중 시험",
            exam_date=self.today + timedelta(days=9),
        )
        Exam.objects.create(
            exam_period=self.exam_period, subject_name="먼저 시험",
            exam_date=self.today + timedelta(days=2),
        )

        response = self._get()
        names = [s["subject_name"] for s in response.context["subject_results"]]

        self.assertEqual(names, ["먼저 시험", "나중 시험"])

    # ── 4. 다른 사용자는 접근 자체가 404 (subject_results 노출 안 됨) ──
    def test_other_user_cannot_access(self):
        from exams.models import Exam

        Exam.objects.create(
            exam_period=self.exam_period, subject_name="비공개 과목",
            exam_date=self.today + timedelta(days=5),
        )
        self.client.logout()
        self.client.login(username="subject_results_other@example.com", password="pass1234")

        response = self._get()

        self.assertEqual(response.status_code, 404)        

class DashboardViewTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        from exams.models import Exam, ExamPeriod, StudyTask

        User = get_user_model()
        self.user = User.objects.create_user(
            username="dashboard_tester", email="dashboard@example.com", password="pass1234"
        )
        self.today = django_timezone.localdate()
        self.client.login(username="dashboard@example.com", password="pass1234")

    def test_dashboard_shows_onboarding_when_no_exam_period(self):
        response = self.client.get(reverse('planner:dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context['exam_period'])

    def test_dashboard_shows_next_step_when_no_plan(self):
        from exams.models import ExamPeriod, Exam, StudyTask

        exam_period = ExamPeriod.objects.create(
            user=self.user, title="테스트 시험기간",
            start_date=self.today, end_date=self.today + timedelta(days=10),
            status="active",
        )
        exam = Exam.objects.create(
            exam_period=exam_period, subject_name="테스트 과목",
            exam_date=self.today + timedelta(days=5),
        )
        StudyTask.objects.create(
            exam=exam, title="작업", importance="high", depth="core",
            task_type="concept", difficulty="normal", order=1,
            estimated_min_minutes=20, estimated_max_minutes=40, is_confirmed=True,
        )

        response = self.client.get(reverse('planner:dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['has_plan'])
        self.assertEqual(response.context['next_step_label'], "계획 생성하기")

    def test_dashboard_shows_next_step_prompting_task_review_when_not_ready(self):
        from exams.models import ExamPeriod, Exam

        exam_period = ExamPeriod.objects.create(
            user=self.user, title="테스트 시험기간",
            start_date=self.today, end_date=self.today + timedelta(days=10),
            status="active",
        )
        Exam.objects.create(
            exam_period=exam_period, subject_name="테스트 과목",
            exam_date=self.today + timedelta(days=5),
        )
        # 과목만 있고 작업이 하나도 없는 상태 -> is_ready False

        response = self.client.get(reverse('planner:dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['has_plan'])
        self.assertEqual(response.context['next_step_label'], "학습 작업 확인하기")

    def test_dashboard_shows_today_count_when_plan_exists(self):
        from exams.models import ExamPeriod, Exam, StudyTask

        exam_period = ExamPeriod.objects.create(
            user=self.user, title="테스트 시험기간",
            start_date=self.today, end_date=self.today + timedelta(days=10),
            status="active",
        )
        exam = Exam.objects.create(
            exam_period=exam_period, subject_name="테스트 과목",
            exam_date=self.today + timedelta(days=5),
        )
        task = StudyTask.objects.create(
            exam=exam, title="작업", importance="high", depth="core",
            task_type="concept", difficulty="normal", order=1,
            estimated_min_minutes=20, estimated_max_minutes=40, is_confirmed=True,
        )
        daily_plan = DailyPlan.objects.create(
            exam_period=exam_period, date=self.today,
            available_minutes=60, planned_minutes=40,
        )
        DailyPlanItem.objects.create(
            daily_plan=daily_plan, study_task=task, planned_minutes=40, order=1,
        )

        response = self.client.get(reverse('planner:dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['has_plan'])
        self.assertEqual(response.context['today_count'], 1)
        self.assertIsNone(response.context['pending_recovery'])

    def test_dashboard_shows_pending_recovery_banner(self):
        from exams.models import ExamPeriod, Exam, StudyTask

        exam_period = ExamPeriod.objects.create(
            user=self.user, title="테스트 시험기간",
            start_date=self.today, end_date=self.today + timedelta(days=10),
            status="active",
        )
        exam = Exam.objects.create(
            exam_period=exam_period, subject_name="테스트 과목",
            exam_date=self.today + timedelta(days=5),
        )
        task = StudyTask.objects.create(
            exam=exam, title="작업", importance="high", depth="core",
            task_type="concept", difficulty="normal", order=1,
            estimated_min_minutes=20, estimated_max_minutes=40, is_confirmed=True,
        )
        daily_plan = DailyPlan.objects.create(
            exam_period=exam_period, date=self.today,
            available_minutes=60, planned_minutes=40,
        )
        item = DailyPlanItem.objects.create(
            daily_plan=daily_plan, study_task=task, planned_minutes=40, order=1,
        )
        record_progress(daily_plan_item=item, status="not_done", actual_minutes=0)

        AvailableTime.objects.create(
            exam_period=exam_period, date=self.today + timedelta(days=1), available_minutes=60,
        )
        finalize_daily_plan(daily_plan)

        response = self.client.get(reverse('planner:dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.context['pending_recovery'])

    def test_dashboard_shows_recovery_retry_banner_when_generation_failed(self):
        from exams.models import ExamPeriod, Exam, StudyTask

        exam_period = ExamPeriod.objects.create(
            user=self.user, title="테스트 시험기간",
            start_date=self.today, end_date=self.today + timedelta(days=10),
            status="active",
        )
        exam = Exam.objects.create(
            exam_period=exam_period, subject_name="테스트 과목",
            exam_date=self.today + timedelta(days=5),
        )
        # importance=high, depth=core -> 핵심집중형에서도 제외 대상이 안 됨
        task = StudyTask.objects.create(
            exam=exam, title="작업", importance="high", depth="core",
            task_type="concept", difficulty="normal", order=1,
            estimated_min_minutes=60, estimated_max_minutes=60, is_confirmed=True,
        )
        daily_plan = DailyPlan.objects.create(
            exam_period=exam_period, date=self.today,
            available_minutes=60, planned_minutes=60,
        )
        item = DailyPlanItem.objects.create(
            daily_plan=daily_plan, study_task=task, planned_minutes=60, order=1,
        )
        record_progress(daily_plan_item=item, status="not_done", actual_minutes=0)
        # 미래 가용시간을 일부러 안 만듦 -> 두 복구 전략 다 실패 -> RecoveryPlan 없음
        finalize_daily_plan(daily_plan)

        response = self.client.get(reverse('planner:dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context['pending_recovery'])
        self.assertEqual(response.context['retry_daily_plan_id'], daily_plan.id)
        self.assertContains(response, "복구안 다시 생성")

    def test_dashboard_does_not_show_other_user_exam_period(self):
        from django.contrib.auth import get_user_model
        from exams.models import ExamPeriod

        User = get_user_model()
        other_user = User.objects.create_user(
            username="other_dashboard", email="other_dashboard@example.com", password="pass1234"
        )
        ExamPeriod.objects.create(
            user=other_user, title="다른 사람 시험기간",
            start_date=self.today, end_date=self.today + timedelta(days=10),
            status="active",
        )

        response = self.client.get(reverse('planner:dashboard'))
        self.assertIsNone(response.context['exam_period'])

    def test_dashboard_shows_pending_recovery_from_yesterday(self):
        from exams.models import ExamPeriod, Exam, StudyTask

        exam_period = ExamPeriod.objects.create(
            user=self.user, title="테스트 시험기간",
            start_date=self.today - timedelta(days=1), end_date=self.today + timedelta(days=10),
            status="active",
        )
        exam = Exam.objects.create(
            exam_period=exam_period, subject_name="테스트 과목",
            exam_date=self.today + timedelta(days=5),
        )
        task = StudyTask.objects.create(
            exam=exam, title="작업", importance="high", depth="core",
            task_type="concept", difficulty="normal", order=1,
            estimated_min_minutes=20, estimated_max_minutes=40, is_confirmed=True,
        )
        yesterday_plan = DailyPlan.objects.create(
            exam_period=exam_period, date=self.today - timedelta(days=1),
            available_minutes=60, planned_minutes=40,
        )
        item = DailyPlanItem.objects.create(
            daily_plan=yesterday_plan, study_task=task, planned_minutes=40, order=1,
        )
        record_progress(daily_plan_item=item, status="not_done", actual_minutes=0)

        AvailableTime.objects.create(
            exam_period=exam_period, date=self.today + timedelta(days=1), available_minutes=60,
        )
        finalize_daily_plan(yesterday_plan)  # 어제 계획을 오늘 마감 -> 오늘은 아직 today_plan 없음

        response = self.client.get(reverse('planner:dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.context['pending_recovery'])

    def test_dashboard_pending_recovery_count_reflects_multiple_pending_groups(self):
        from exams.models import ExamPeriod, Exam, StudyTask

        exam_period = ExamPeriod.objects.create(
            user=self.user, title="테스트 시험기간",
            start_date=self.today - timedelta(days=2), end_date=self.today + timedelta(days=10),
            status="active",
        )
        exam = Exam.objects.create(
            exam_period=exam_period, subject_name="테스트 과목",
            exam_date=self.today + timedelta(days=5),
        )

        # 복구안은 마감 시점과 무관하게 항상 "오늘+1"부터 배치되므로,
        # 두 마감(plan_1, plan_2) 모두 이 날짜의 가용시간을 참조한다.
        AvailableTime.objects.create(
            exam_period=exam_period, date=self.today + timedelta(days=1), available_minutes=100,
        )

        task_1 = StudyTask.objects.create(
            exam=exam, title="작업1", importance="high", depth="core",
            task_type="concept", difficulty="normal", order=1,
            estimated_min_minutes=20, estimated_max_minutes=40, is_confirmed=True,
        )
        plan_1 = DailyPlan.objects.create(
            exam_period=exam_period, date=self.today - timedelta(days=2),
            available_minutes=60, planned_minutes=40,
        )
        item_1 = DailyPlanItem.objects.create(
            daily_plan=plan_1, study_task=task_1, planned_minutes=40, order=1,
        )
        record_progress(daily_plan_item=item_1, status="not_done", actual_minutes=0)
        finalize_daily_plan(plan_1)

        task_2 = StudyTask.objects.create(
            exam=exam, title="작업2", importance="high", depth="core",
            task_type="concept", difficulty="normal", order=2,
            estimated_min_minutes=20, estimated_max_minutes=40, is_confirmed=True,
        )
        plan_2 = DailyPlan.objects.create(
            exam_period=exam_period, date=self.today - timedelta(days=1),
            available_minutes=60, planned_minutes=40,
        )
        item_2 = DailyPlanItem.objects.create(
            daily_plan=plan_2, study_task=task_2, planned_minutes=40, order=1,
        )
        record_progress(daily_plan_item=item_2, status="not_done", actual_minutes=0)
        finalize_daily_plan(plan_2)

        response = self.client.get(reverse('planner:dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.context['pending_recovery'])
        self.assertEqual(response.context['pending_recovery']['count'], 2)

class TodayViewTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        self.user = User.objects.create_user(
            username="today_tester", email="today@example.com", password="pass1234"
        )
        self.today = django_timezone.localdate()
        self.client.login(username="today@example.com", password="pass1234")

    def _make_period_exam_task(self, order=1, min_m=20, max_m=40):
        from exams.models import ExamPeriod, Exam, StudyTask
        exam_period = ExamPeriod.objects.create(
            user=self.user, title="테스트 시험기간",
            start_date=self.today, end_date=self.today + timedelta(days=10),
            status="active",
        )
        exam = Exam.objects.create(
            exam_period=exam_period, subject_name="테스트 과목",
            exam_date=self.today + timedelta(days=5),
        )
        task = StudyTask.objects.create(
            exam=exam, title="작업", importance="high", depth="core",
            task_type="concept", difficulty="normal", order=order,
            estimated_min_minutes=min_m, estimated_max_minutes=max_m, is_confirmed=True,
        )
        return exam_period, exam, task

    def test_today_shows_onboarding_when_no_exam_period(self):
        response = self.client.get(reverse('planner:today'))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context['exam_period'])
        self.assertEqual(response.context['tasks'], [])

    def test_today_shows_empty_when_no_plan_today(self):
        self._make_period_exam_task()
        response = self.client.get(reverse('planner:today'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['tasks'], [])

    def test_today_remaining_minutes_for_done_task(self):
        exam_period, exam, task = self._make_period_exam_task(min_m=20, max_m=40)
        daily_plan = DailyPlan.objects.create(
            exam_period=exam_period, date=self.today, available_minutes=60, planned_minutes=40,
        )
        item = DailyPlanItem.objects.create(
            daily_plan=daily_plan, study_task=task, planned_minutes=40, order=1,
        )
        # 완료인데 실제 시간이 계획보다 큼 -> done은 planned 기준으로 전액 제외돼야 함
        record_progress(daily_plan_item=item, status="done", actual_minutes=90)

        response = self.client.get(reverse('planner:today'))
        self.assertEqual(response.context['summary']['remaining_minutes'], 0)
        self.assertEqual(response.context['summary']['done_minutes'], 40)
        self.assertEqual(response.context['eod']['done_minutes'], 90)  # eod는 실제 시간

    def test_today_remaining_minutes_for_partial_task_uses_completion_percent(self):
        exam_period, exam, task = self._make_period_exam_task(min_m=20, max_m=40)
        daily_plan = DailyPlan.objects.create(
            exam_period=exam_period, date=self.today, available_minutes=60, planned_minutes=40,
        )
        item = DailyPlanItem.objects.create(
            daily_plan=daily_plan, study_task=task, planned_minutes=40, order=1,
        )
        # 일부완료, 완료율 50%, 근데 실제 시간은 계획(40)보다 훨씬 큼(90)
        record_progress(
            daily_plan_item=item, status="partial", actual_minutes=90, completion_percent=50,
        )

        response = self.client.get(reverse('planner:today'))
        # 남은 시간은 completion_percent 기준(40 * 50% = 20)이어야지, actual_minutes로 계산하면 안 됨
        self.assertEqual(response.context['summary']['remaining_minutes'], 20)
        self.assertEqual(response.context['summary']['partial_minutes'], 20)
        self.assertEqual(response.context['eod']['partial_minutes'], 90)  # eod는 실제 시간

    def test_today_does_not_show_other_user_plan(self):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        other_user = User.objects.create_user(
            username="other_today", email="other_today@example.com", password="pass1234"
        )
        from exams.models import ExamPeriod
        ExamPeriod.objects.create(
            user=other_user, title="다른 사람 시험기간",
            start_date=self.today, end_date=self.today + timedelta(days=10),
            status="active",
        )
        response = self.client.get(reverse('planner:today'))
        self.assertIsNone(response.context['exam_period'])

    def test_today_shows_finalized_state(self):
        exam_period, exam, task = self._make_period_exam_task()
        daily_plan = DailyPlan.objects.create(
            exam_period=exam_period, date=self.today, available_minutes=60, planned_minutes=40,
        )
        item = DailyPlanItem.objects.create(
            daily_plan=daily_plan, study_task=task, planned_minutes=40, order=1,
        )
        record_progress(daily_plan_item=item, status="done", actual_minutes=40)
        finalize_daily_plan(daily_plan)

        response = self.client.get(reverse('planner:today'))
        self.assertTrue(response.context['is_finalized'])

    def test_today_shows_pending_recovery_in_sidebar_context(self):
        exam_period, exam, task = self._make_period_exam_task()
        daily_plan = DailyPlan.objects.create(
            exam_period=exam_period, date=self.today, available_minutes=60, planned_minutes=40,
        )
        item = DailyPlanItem.objects.create(
            daily_plan=daily_plan, study_task=task, planned_minutes=40, order=1,
        )
        record_progress(daily_plan_item=item, status="not_done", actual_minutes=0)
        AvailableTime.objects.create(
            exam_period=exam_period, date=self.today + timedelta(days=1), available_minutes=60,
        )
        finalize_daily_plan(daily_plan)

        response = self.client.get(reverse('planner:today'))
        self.assertIsNotNone(response.context['pending_recovery'])

    def test_today_eod_includes_pending_as_not_done(self):
        exam_period, exam, task = self._make_period_exam_task()
        daily_plan = DailyPlan.objects.create(
            exam_period=exam_period, date=self.today, available_minutes=60, planned_minutes=40,
        )
        DailyPlanItem.objects.create(
            daily_plan=daily_plan, study_task=task, planned_minutes=40, order=1,
        )
        # 진행 기록 아예 입력 안 한 상태

        response = self.client.get(reverse('planner:today'))
        self.assertEqual(response.context['eod']['pending_count'], 1)
        self.assertEqual(response.context['eod']['not_done_count'], 1)

class DailyPlanFinalizeViewTests(TestCase):
    """
    #79 daily_plan_finalize API 테스트.

    로그인한 사용자의 ACTIVE 시험기간에 속한 오늘 DailyPlan을 조회하고,
    finalize_daily_plan()을 호출해 하루 계획을 마감하는 JSON API를 검증한다.
    """

    def setUp(self):
        from django.contrib.auth import get_user_model

        User = get_user_model()

        self.user = User.objects.create_user(
            username="finalize_view_tester",
            email="finalize_view@example.com",
            password="pass1234",
        )
        self.today = django_timezone.localdate()

        self.client.login(
            username="finalize_view@example.com",
            password="pass1234",
        )
        self.url = reverse("planner:daily_plan_finalize")

    def _make_active_exam_period(self, user=None, title="마감 테스트 시험기간"):
        from exams.models import ExamPeriod

        return ExamPeriod.objects.create(
            user=user or self.user,
            title=title,
            start_date=self.today - timedelta(days=1),
            end_date=self.today + timedelta(days=10),
            status="active",
        )

    def _make_exam(self, exam_period, exam_date=None):
        from exams.models import Exam

        return Exam.objects.create(
            exam_period=exam_period,
            subject_name="테스트 과목",
            exam_date=exam_date or self.today + timedelta(days=5),
        )

    def _make_task(self, exam, importance="high", depth="core"):
        from exams.models import StudyTask

        return StudyTask.objects.create(
            exam=exam,
            title="마감 대상 작업",
            importance=importance,
            depth=depth,
            task_type="concept",
            difficulty="normal",
            order=1,
            estimated_min_minutes=20,
            estimated_max_minutes=40,
            is_confirmed=True,
        )

    def _make_today_plan_with_item(
        self,
        exam_period,
        importance="high",
        depth="core",
    ):
        exam = self._make_exam(exam_period)
        task = self._make_task(
            exam,
            importance=importance,
            depth=depth,
        )

        daily_plan = DailyPlan.objects.create(
            exam_period=exam_period,
            date=self.today,
            available_minutes=40,
            planned_minutes=40,
        )
        item = DailyPlanItem.objects.create(
            daily_plan=daily_plan,
            study_task=task,
            planned_minutes=40,
            order=1,
        )

        return daily_plan, item

    def test_no_active_exam_period_returns_404(self):
        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json()["message"],
            "오늘 마감할 계획이 없습니다.",
        )

    def test_no_today_plan_returns_404(self):
        self._make_active_exam_period()

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json()["message"],
            "오늘 마감할 계획이 없습니다.",
        )

    def test_other_users_today_plan_returns_404(self):
        from django.contrib.auth import get_user_model

        User = get_user_model()

        other_user = User.objects.create_user(
            username="other_finalize_tester",
            email="other_finalize@example.com",
            password="pass1234",
        )
        other_period = self._make_active_exam_period(user=other_user)
        self._make_today_plan_with_item(other_period)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json()["message"],
            "오늘 마감할 계획이 없습니다.",
        )

    def test_all_done_finalize_needs_recovery_false(self):
        exam_period = self._make_active_exam_period()
        daily_plan, item = self._make_today_plan_with_item(exam_period)

        record_progress(
            daily_plan_item=item,
            status="done",
            actual_minutes=35,
        )

        response = self.client.post(self.url)
        body = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(body["needs_recovery"])
        self.assertFalse(body["recovery_available"])
        self.assertIsNone(body["recovery_group_id"])
        self.assertEqual(body["auto_marked_not_done_count"], 0)

        daily_plan.refresh_from_db()
        self.assertIsNotNone(daily_plan.finalized_at)

    def test_partial_or_not_done_needs_recovery_true(self):
        exam_period = self._make_active_exam_period()
        _daily_plan, item = self._make_today_plan_with_item(
            exam_period,
            importance="low",
            depth="optional",
        )

        record_progress(
            daily_plan_item=item,
            status="not_done",
            actual_minutes=0,
        )
        AvailableTime.objects.create(
            exam_period=exam_period,
            date=self.today + timedelta(days=1),
            available_minutes=60,
        )

        response = self.client.post(self.url)
        body = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(body["needs_recovery"])

    def test_unrecorded_task_auto_marked_not_done(self):
        exam_period = self._make_active_exam_period()
        _daily_plan, item = self._make_today_plan_with_item(
            exam_period,
            importance="low",
            depth="optional",
        )
        AvailableTime.objects.create(
            exam_period=exam_period,
            date=self.today + timedelta(days=1),
            available_minutes=60,
        )

        response = self.client.post(self.url)
        body = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["auto_marked_not_done_count"], 1)
        self.assertTrue(body["needs_recovery"])

        item.refresh_from_db()
        self.assertEqual(
            item.progress_log.progress_status,
            "not_done",
        )

    def test_recovery_available_true_returns_group_id(self):
        exam_period = self._make_active_exam_period()
        _daily_plan, item = self._make_today_plan_with_item(
            exam_period,
            importance="low",
            depth="optional",
        )

        record_progress(
            daily_plan_item=item,
            status="not_done",
            actual_minutes=0,
        )
        AvailableTime.objects.create(
            exam_period=exam_period,
            date=self.today + timedelta(days=1),
            available_minutes=60,
        )

        response = self.client.post(self.url)
        body = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(body["recovery_available"])
        self.assertIsNotNone(body["recovery_group_id"])

        self.assertTrue(
            RecoveryPlan.objects.filter(
                recovery_group_id=body["recovery_group_id"],
            ).exists()
        )

    def test_recovery_unavailable_when_both_types_fail(self):
        exam_period = self._make_active_exam_period()

        # high/core 작업은 핵심 집중형 제외 후보가 아니다.
        # 미래 가용시간도 없으므로 분량 유지형도 생성되지 않는다.
        _daily_plan, item = self._make_today_plan_with_item(
            exam_period,
            importance="high",
            depth="core",
        )

        record_progress(
            daily_plan_item=item,
            status="not_done",
            actual_minutes=0,
        )

        response = self.client.post(self.url)
        body = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(body["needs_recovery"])
        self.assertFalse(body["recovery_available"])
        self.assertIsNone(body["recovery_group_id"])

    def test_already_finalized_returns_409(self):
        exam_period = self._make_active_exam_period()
        _daily_plan, item = self._make_today_plan_with_item(exam_period)

        record_progress(
            daily_plan_item=item,
            status="done",
            actual_minutes=35,
        )

        first_response = self.client.post(self.url)
        self.assertEqual(first_response.status_code, 200)

        second_response = self.client.post(self.url)

        self.assertEqual(second_response.status_code, 409)
        self.assertEqual(
            second_response.json()["message"],
            "이미 마감된 계획입니다.",
        )

    def test_get_method_not_allowed(self):
        exam_period = self._make_active_exam_period()
        self._make_today_plan_with_item(exam_period)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 405)

    def test_finds_today_plan_in_older_active_exam_period(self):
        """
        더 최근에 생성된 ACTIVE 시험기간에는 오늘 계획이 없고,
        이전 ACTIVE 시험기간에만 오늘 계획이 있어도 정상 마감해야 한다.
        """
        from exams.models import ExamPeriod

        older_period = self._make_active_exam_period(
            title="이전 시험기간",
        )
        daily_plan, item = self._make_today_plan_with_item(older_period)

        newer_period = self._make_active_exam_period(
            title="최근 시험기간",
        )

        # 생성 시각의 우선순위를 명확하게 만든다.
        ExamPeriod.objects.filter(pk=older_period.pk).update(
            created_at=django_timezone.now() - timedelta(minutes=1),
        )
        ExamPeriod.objects.filter(pk=newer_period.pk).update(
            created_at=django_timezone.now(),
        )

        record_progress(
            daily_plan_item=item,
            status="done",
            actual_minutes=35,
        )

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 200)

        daily_plan.refresh_from_db()
        self.assertIsNotNone(daily_plan.finalized_at)

class ProgressRecordViewTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        from exams.models import ExamPeriod, Exam, StudyTask

        User = get_user_model()
        self.user = User.objects.create_user(
            username="progress_view_tester", email="progressview@example.com", password="pass1234"
        )
        self.other_user = User.objects.create_user(
            username="other_progress_view", email="other_progressview@example.com", password="pass1234"
        )
        self.today = django_timezone.localdate()
        self.exam_period = ExamPeriod.objects.create(
            user=self.user, title="테스트 시험기간",
            start_date=self.today, end_date=self.today + timedelta(days=10),
            status="active",
        )
        self.exam = Exam.objects.create(
            exam_period=self.exam_period, subject_name="테스트 과목",
            exam_date=self.today + timedelta(days=5),
        )
        self.task = StudyTask.objects.create(
            exam=self.exam, title="작업", importance="high", depth="core",
            task_type="concept", difficulty="normal", order=1,
            estimated_min_minutes=20, estimated_max_minutes=40, is_confirmed=True,
        )
        self.daily_plan = DailyPlan.objects.create(
            exam_period=self.exam_period, date=self.today,
            available_minutes=60, planned_minutes=40,
        )
        self.item = DailyPlanItem.objects.create(
            daily_plan=self.daily_plan, study_task=self.task, planned_minutes=40, order=1,
        )
        self.client.login(username="progressview@example.com", password="pass1234")

    def _post(self, item_id, payload):
        import json
        return self.client.post(
            reverse('planner:progress_record', kwargs={'item_id': item_id}),
            data=json.dumps(payload),
            content_type='application/json',
        )

    def test_record_done_status(self):
        response = self._post(self.item.id, {"status": "done", "actual_minutes": 45})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['status'], 'done')
        self.assertEqual(data['actual_minutes'], 45)

    def test_record_partial_status(self):
        response = self._post(
            self.item.id, {"status": "partial", "actual_minutes": 20, "completion_percent": 50}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['completion_percent'], 50)

    def test_record_not_done_status(self):
        response = self._post(self.item.id, {"status": "not_done"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['actual_minutes'], 0)

    def test_invalid_completion_percent_rejected(self):
        response = self._post(
            self.item.id, {"status": "partial", "actual_minutes": 20, "completion_percent": 0}
        )
        self.assertEqual(response.status_code, 400)

    def test_string_actual_minutes_rejected_not_500(self):
        response = self._post(self.item.id, {"status": "done", "actual_minutes": "30"})
        self.assertEqual(response.status_code, 400)

    def test_float_completion_percent_rejected(self):
        response = self._post(
            self.item.id, {"status": "partial", "actual_minutes": 10, "completion_percent": 50.5}
        )
        self.assertEqual(response.status_code, 400)

    def test_non_dict_body_rejected_not_500(self):
        response = self.client.post(
            reverse('planner:progress_record', kwargs={'item_id': self.item.id}),
            data='[]',
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)

    def test_resubmit_updates_existing_record_not_duplicate(self):
        self._post(self.item.id, {"status": "partial", "actual_minutes": 20, "completion_percent": 50})
        response = self._post(self.item.id, {"status": "done", "actual_minutes": 40})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ProgressLog.objects.filter(daily_plan_item=self.item).count(), 1)

    def test_rejects_other_user_item_with_json_404(self):
        self.client.login(username="other_progressview@example.com", password="pass1234")
        response = self._post(self.item.id, {"status": "done", "actual_minutes": 30})
        self.assertEqual(response.status_code, 404)
        # HTML이 아니라 JSON으로 응답하는지 확인
        data = response.json()
        self.assertIn('message', data)

    def test_rejects_non_today_plan_item(self):
        future_plan = DailyPlan.objects.create(
            exam_period=self.exam_period, date=self.today + timedelta(days=1),
            available_minutes=60, planned_minutes=40,
        )
        future_item = DailyPlanItem.objects.create(
            daily_plan=future_plan, study_task=self.task, planned_minutes=40, order=1,
        )
        response = self._post(future_item.id, {"status": "done", "actual_minutes": 30})
        self.assertEqual(response.status_code, 404)

    def test_rejects_edit_after_finalized(self):
        self._post(self.item.id, {"status": "not_done"})
        AvailableTime.objects.create(
            exam_period=self.exam_period, date=self.today + timedelta(days=1), available_minutes=60,
        )
        finalize_daily_plan(self.daily_plan)

        response = self._post(self.item.id, {"status": "done", "actual_minutes": 40})
        self.assertEqual(response.status_code, 409)

    def test_speed_factor_updated_after_record(self):
        self._post(self.item.id, {"status": "done", "actual_minutes": 60})
        self.exam.refresh_from_db()
        self.assertNotEqual(self.exam.speed_factor, 1.0)

class CalendarViewTests(TestCase):
    """
    기준으로 View + build_calendar_context()를 검증한다.
    """

    def setUp(self):
        from django.contrib.auth import get_user_model

        User = get_user_model()

        self.user = User.objects.create_user(
            username="calendar_tester", email="calendar_tester@example.com",
            password="pass1234",
        )
        self.other = User.objects.create_user(
            username="calendar_other", email="calendar_other@example.com",
            password="pass1234",
        )
        self.today = django_timezone.localdate()
        self.client.login(username="calendar_tester@example.com", password="pass1234")
        self.url = reverse("planner:calendar")

    def _make_active_exam_period(self, user=None):
        from exams.models import ExamPeriod

        return ExamPeriod.objects.create(
            user=user or self.user, title="캘린더 테스트 시험기간",
            start_date=self.today - timedelta(days=3),
            end_date=self.today + timedelta(days=10),
            status="active",
        )

    def test_shows_onboarding_when_no_exam_period(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["exam_period"])

    def test_defaults_to_current_month_when_no_query_params(self):
        self._make_active_exam_period()
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["year"], self.today.year)
        self.assertEqual(response.context["month"], self.today.month)

    def test_invalid_month_falls_back_to_current_month(self):
        self._make_active_exam_period()
        response = self.client.get(self.url, {"year": 2026, "month": 13})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["month"], self.today.month)

    def test_invalid_year_falls_back_to_current_year(self):
        self._make_active_exam_period()

        for bad_year in (0, 10000):
            response = self.client.get(self.url, {"year": bad_year, "month": 8})

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.context["year"], self.today.year)
            self.assertEqual(response.context["month"], self.today.month)

    def test_boundary_year_month_navigation_links_use_normalized_month(self):
        """
        리뷰 반영: year=9999, month=12는 build_calendar_context() 내부에서
        오늘 날짜로 보정되지만(그래야 6주 격자 패딩이 연도 경계를 안 넘음),
        View가 그 보정된 값을 다시 받아오지 않으면 화면 제목(year/month)은
        정상인데 "다음 달" 링크(next_year/next_month)는 여전히 9999/12
        기준(예: next_year=10000)으로 깨질 수 있었다. 이제는 prev/next도
        보정된 값 기준으로 계산되어야 한다.
        """
        self._make_active_exam_period()

        response = self.client.get(self.url, {"year": 9999, "month": 12})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["year"], self.today.year)
        self.assertEqual(response.context["month"], self.today.month)
        # next_month가 1~12 범위를 벗어나거나(예: 13), next_year가 10000처럼
        # 깨진 값이면 안 된다.
        self.assertTrue(1 <= response.context["next_month"] <= 12)
        self.assertTrue(1 <= response.context["prev_month"] <= 12)
        self.assertNotEqual(response.context["next_year"], 10000)

    def test_week_grid_has_six_weeks_of_seven_days(self):
        self._make_active_exam_period()
        response = self.client.get(
            self.url, {"year": self.today.year, "month": self.today.month}
        )

        weeks = response.context["weeks"]
        self.assertEqual(len(weeks), 6)
        for week in weeks:
            self.assertEqual(len(week), 7)

    def test_date_outside_period_marked_not_in_period(self):
        period = self._make_active_exam_period()
        response = self.client.get(
            self.url, {"year": self.today.year, "month": self.today.month}
        )

        outside_date = period.start_date - timedelta(days=1)
        cell = self._find_cell(response.context["weeks"], outside_date)
        if cell is not None:  # 달력에 그 날짜가 안 나온 달이면 스킵
            self.assertFalse(cell["in_period"])

    def test_exam_day_shows_subject_and_no_load_bar(self):
        from exams.models import Exam

        period = self._make_active_exam_period()
        exam_date = self.today + timedelta(days=2)
        Exam.objects.create(
            exam_period=period, subject_name="데이터통신", exam_date=exam_date,
        )
        response = self.client.get(
            self.url, {"year": exam_date.year, "month": exam_date.month}
        )

        cell = self._find_cell(response.context["weeks"], exam_date)
        self.assertTrue(cell["is_exam_day"])
        self.assertEqual(cell["exam_subject"], "데이터통신")
        self.assertEqual(cell["available_minutes"], 0)

    def test_load_percent_over_100_marks_is_over(self):
        from exams.models import Exam
        from planner.models import DailyPlan, DailyPlanItem

        period = self._make_active_exam_period()
        exam = Exam.objects.create(
            exam_period=period, subject_name="운영체제",
            exam_date=self.today + timedelta(days=5),
        )
        task = exam.study_tasks.create(
            unit_name="1장", title="1장 정리", task_type="concept",
            importance="high", depth="core", difficulty="normal",
            estimated_min_minutes=30, estimated_max_minutes=50, order=1,
        )
        target_date = self.today + timedelta(days=1)
        AvailableTime.objects.create(
            exam_period=period, date=target_date, available_minutes=100,
        )
        daily_plan = DailyPlan.objects.create(
            exam_period=period, date=target_date,
            available_minutes=100, planned_minutes=150,
        )
        DailyPlanItem.objects.create(
            daily_plan=daily_plan, study_task=task, planned_minutes=150, order=1,
        )

        response = self.client.get(
            self.url, {"year": target_date.year, "month": target_date.month}
        )

        cell = self._find_cell(response.context["weeks"], target_date)
        self.assertEqual(cell["load_percent"], 150)
        self.assertTrue(cell["is_over"])

    def test_day_details_matches_task_row_fields(self):
        from exams.models import Exam
        from planner.models import DailyPlan, DailyPlanItem

        period = self._make_active_exam_period()
        exam = Exam.objects.create(
            exam_period=period, subject_name="신호및시스템",
            exam_date=self.today + timedelta(days=5),
        )
        task = exam.study_tasks.create(
            unit_name="2장", title="2장 예제 풀이", task_type="practice",
            importance="medium", depth="basic", difficulty="easy",
            estimated_min_minutes=20, estimated_max_minutes=40, order=1,
        )
        target_date = self.today + timedelta(days=1)
        daily_plan = DailyPlan.objects.create(
            exam_period=period, date=target_date,
            available_minutes=120, planned_minutes=40,
        )
        DailyPlanItem.objects.create(
            daily_plan=daily_plan, study_task=task, planned_minutes=40, order=1,
        )

        response = self.client.get(
            self.url, {"year": target_date.year, "month": target_date.month}
        )

        day_detail = response.context["day_details"][target_date.isoformat()]
        task_data = day_detail["tasks"][0]
        self.assertEqual(
            set(task_data.keys()),
            {"title", "subject_name", "depth", "planned_minutes",
             "status", "actual_minutes", "completion_percent"},
        )
        self.assertEqual(task_data["title"], "2장 예제 풀이")
        self.assertEqual(task_data["subject_name"], "신호및시스템")

    def test_shade_index_ordered_by_exam_date(self):
        from exams.models import Exam, AvailableTime as AT
        from planner.models import DailyPlan, DailyPlanItem
        from planner.services.calendar import build_calendar_context

        period = self._make_active_exam_period()
        Exam.objects.create(
            exam_period=period, subject_name="늦은 시험",
            exam_date=self.today + timedelta(days=9),
        )
        earlier_exam = Exam.objects.create(
            exam_period=period, subject_name="빠른 시험",
            exam_date=self.today + timedelta(days=2),
        )
        task = earlier_exam.study_tasks.create(
            unit_name="1장", title="빠른 시험 작업", task_type="concept",
            importance="high", depth="core", difficulty="normal",
            estimated_min_minutes=30, estimated_max_minutes=50, order=1,
        )
        target_date = self.today + timedelta(days=1)
        daily_plan = DailyPlan.objects.create(
            exam_period=period, date=target_date,
            available_minutes=100, planned_minutes=30,
        )
        DailyPlanItem.objects.create(
            daily_plan=daily_plan, study_task=task, planned_minutes=30, order=1,
        )

        data = build_calendar_context(period, target_date.year, target_date.month)
        cell = self._find_cell(data["weeks"], target_date)
        # 더 이른 시험(earlier_exam)의 작업이니 shade_index=0 이어야 한다
        self.assertEqual(cell["tasks"][0]["shade_index"], 0)

    def test_other_user_exam_period_not_used(self):
        self._make_active_exam_period(user=self.other)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["exam_period"])

    @staticmethod

    def _find_cell(weeks, target_date):
        for week in weeks:
            for cell in week:
                if cell["date"] == target_date:
                    return cell
        return None
    
class RecoveryCompareViewTests(TestCase):
    """
    #81 recovery_compare View 테스트.

    RecoveryPlan/RecoveryPlanItem을 직접 만들어서 View 단위로만 검증한다
    (generate_recovery_options()를 거치는 통합 검증은 FinalizeDailyPlanTests가 이미 담당).
    """

    def setUp(self):
        from django.contrib.auth import get_user_model
        from exams.models import ExamPeriod, Exam, StudyTask

        User = get_user_model()
        self.user = User.objects.create_user(
            username="recovery_compare_tester",
            email="recovery_compare@example.com",
            password="pass1234",
        )
        self.other_user = User.objects.create_user(
            username="recovery_compare_other",
            email="recovery_compare_other@example.com",
            password="pass1234",
        )
        self.client.login(username="recovery_compare@example.com", password="pass1234")

        self.today = django_timezone.localdate()
        self.exam_period = ExamPeriod.objects.create(
            user=self.user,
            title="복구안 비교 테스트 시험기간",
            start_date=self.today - timedelta(days=1),
            end_date=self.today + timedelta(days=10),
            status="active",
        )
        self.exam = Exam.objects.create(
            exam_period=self.exam_period,
            subject_name="테스트 과목",
            exam_date=self.today + timedelta(days=7),
            speed_factor=1.0,
        )
        self.task = StudyTask.objects.create(
            exam=self.exam,
            title="복구 대상 작업",
            importance="low",
            depth="optional",
            task_type="practice",
            difficulty="normal",
            order=1,
            estimated_min_minutes=30,
            estimated_max_minutes=60,
            is_confirmed=True,
        )
        self.daily_plan = DailyPlan.objects.create(
            exam_period=self.exam_period,
            date=self.today,
            available_minutes=60,
            planned_minutes=60,
        )

    def _make_recovery_plan(self, exam_period, recovery_type,
                            recovery_group_id, status=RecoveryPlanStatus.PENDING,
                            with_reschedule_item=True):
        plan = RecoveryPlan.objects.create(
            exam_period=exam_period,
            source_daily_plan=self.daily_plan,
            recovery_group_id=recovery_group_id,
            recovery_type=recovery_type,
            status=status,
        )
        if with_reschedule_item:
            RecoveryPlanItem.objects.create(
                recovery_plan=plan,
                study_task=self.task,
                original_date=self.today,
                changed_date=self.today + timedelta(days=1),
                action_type=RecoveryActionType.RESCHEDULE,
                remaining_minutes=60,
                reason="테스트용 재배치",
            )
        return plan

    def _make_both_plans(self, exam_period=None, group_id=None):
        group_id = group_id or uuid.uuid4()
        exam_period = exam_period or self.exam_period
        maintain = self._make_recovery_plan(
            exam_period, RecoveryType.MAINTAIN_VOLUME, group_id,
        )
        core_focus = self._make_recovery_plan(
            exam_period, RecoveryType.CORE_FOCUS, group_id,
        )
        return group_id, maintain, core_focus

    def test_preview_url_included_in_plan_context(self):
        group_id, maintain, core_focus = self._make_both_plans()

        response = self._get(group_id)
        plans = response.context["plans"]

        expected = reverse("planner:recovery_preview", kwargs={"plan_id": maintain.id})
        self.assertEqual(plans[0]["preview_url"], expected)


    def _get(self, group_id):
        return self.client.get(
            reverse("planner:recovery_compare", kwargs={"group_id": group_id})
        )

    # ── 1. 정상 케이스: 두 복구안 모두 조회 ──────────────────
    def test_returns_both_plans_when_both_exist(self):
        group_id, maintain, core_focus = self._make_both_plans()

        response = self._get(group_id)

        self.assertEqual(response.status_code, 200)
        plans = response.context["plans"]
        self.assertEqual(len(plans), 2)
        self.assertEqual(plans[0]["recovery_type"], RecoveryType.MAINTAIN_VOLUME)
        self.assertEqual(plans[1]["recovery_type"], RecoveryType.CORE_FOCUS)

    # ── 2. 한쪽 복구안만 존재해도 200 렌더 ─────────────────────
    def test_returns_maintain_only_when_core_focus_missing(self):
        group_id = uuid.uuid4()
        self._make_recovery_plan(self.exam_period, RecoveryType.MAINTAIN_VOLUME, group_id)

        response = self._get(group_id)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["plans"]), 1)
        self.assertEqual(
            response.context["plans"][0]["recovery_type"], RecoveryType.MAINTAIN_VOLUME
        )

    def test_returns_core_focus_only_when_maintain_missing(self):
        group_id = uuid.uuid4()
        self._make_recovery_plan(self.exam_period, RecoveryType.CORE_FOCUS, group_id)

        response = self._get(group_id)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["plans"]), 1)
        self.assertEqual(
            response.context["plans"][0]["recovery_type"], RecoveryType.CORE_FOCUS
        )

    # ── 3. 그룹 자체가 존재하지 않으면 404 ─────────────────
    def test_404_when_group_does_not_exist(self):
        response = self._get(uuid.uuid4())
        self.assertEqual(response.status_code, 404)

    # ── 4. 다른 사용자의 복구안은 조회 불가 ─────────────────
    def test_404_when_owned_by_other_user(self):
        from exams.models import ExamPeriod

        other_period = ExamPeriod.objects.create(
            user=self.other_user,
            title="다른 사용자 시험기간",
            start_date=self.today - timedelta(days=1),
            end_date=self.today + timedelta(days=10),
            status="active",
        )
        group_id, _, _ = self._make_both_plans(exam_period=other_period)

        response = self._get(group_id)

        self.assertEqual(response.status_code, 404)

    # ── 5. PENDING이 아닌 복구안(이미 처리됨)은 비교 대상에서 제외 ──
    def test_404_when_plans_already_applied(self):
        group_id = uuid.uuid4()
        self._make_recovery_plan(
            self.exam_period, RecoveryType.MAINTAIN_VOLUME, group_id,
            status=RecoveryPlanStatus.APPLIED,
        )
        self._make_recovery_plan(
            self.exam_period, RecoveryType.CORE_FOCUS, group_id,
            status=RecoveryPlanStatus.DISCARDED,
        )

        response = self._get(group_id)

        self.assertEqual(response.status_code, 404)

    # ── 6. Fit Bar가 두 카드 공통 axis_max를 쓰는지 확인 ─────
    def test_fit_bar_shares_axis_max_across_plans(self):
        group_id, maintain, core_focus = self._make_both_plans()

        response = self._get(group_id)
        plans = response.context["plans"]

        self.assertEqual(plans[0]["axis_max"], plans[1]["axis_max"])
        self.assertGreater(plans[0]["axis_max"], 0)

    # ── 7. 제외된 작업이 excluded_tasks에 정확히 반영되는지 ──
    def test_excluded_task_appears_in_core_focus_only(self):
        group_id = uuid.uuid4()
        maintain = self._make_recovery_plan(
            self.exam_period, RecoveryType.MAINTAIN_VOLUME, group_id,
        )
        core_focus = RecoveryPlan.objects.create(
            exam_period=self.exam_period,
            source_daily_plan=self.daily_plan,
            recovery_group_id=group_id,
            recovery_type=RecoveryType.CORE_FOCUS,
            status=RecoveryPlanStatus.PENDING,
        )
        RecoveryPlanItem.objects.create(
            recovery_plan=core_focus,
            study_task=self.task,
            original_date=self.today,
            changed_date=None,
            action_type=RecoveryActionType.EXCLUDE,
            remaining_minutes=60,
            reason="핵심 집중형: 우선순위 낮은 작업 단계적 제외",
        )

        response = self._get(group_id)
        plans_by_type = {p["recovery_type"]: p for p in response.context["plans"]}

        self.assertEqual(plans_by_type[RecoveryType.MAINTAIN_VOLUME]["excluded_count"], 0)
        self.assertEqual(plans_by_type[RecoveryType.CORE_FOCUS]["excluded_count"], 1)
        self.assertEqual(
            plans_by_type[RecoveryType.CORE_FOCUS]["excluded_tasks"][0]["title"],
            self.task.title,
        )

    # ── 8. exam_period가 context에 있는지 (사이드바 렌더링용) ──
    def test_exam_period_in_context(self):
        group_id, _, _ = self._make_both_plans()

        response = self._get(group_id)

        self.assertEqual(response.context["exam_period"], self.exam_period)

    # ── 9. 로그인 안 하면 로그인 페이지로 리다이렉트 ─────────
    def test_requires_login(self):
        self.client.logout()
        group_id, _, _ = self._make_both_plans()

        response = self._get(group_id)

        self.assertEqual(response.status_code, 302)

class RecoveryPreviewViewTests(TestCase):
    """
    #81 recovery_preview View 테스트.
    """

    def setUp(self):
        from django.contrib.auth import get_user_model
        from exams.models import ExamPeriod, Exam, StudyTask, AvailableTime

        User = get_user_model()
        self.user = User.objects.create_user(
            username="recovery_preview_tester",
            email="recovery_preview@example.com",
            password="pass1234",
        )
        self.other_user = User.objects.create_user(
            username="recovery_preview_other",
            email="recovery_preview_other@example.com",
            password="pass1234",
        )
        self.client.login(username="recovery_preview@example.com", password="pass1234")

        self.today = django_timezone.localdate()
        self.tomorrow = self.today + timedelta(days=1)
        self.day2 = self.today + timedelta(days=2)

        self.exam_period = ExamPeriod.objects.create(
            user=self.user,
            title="복구 미리보기 테스트 시험기간",
            start_date=self.today - timedelta(days=1),
            end_date=self.today + timedelta(days=10),
            status="active",
        )
    
        # near_exam: 시험일이 tomorrow인 과목. 이 과목 소속 작업은 tomorrow에 배치될 수 없다
        # (scheduler.py: day >= task.exam_date면 배치 불가).
        self.near_exam = Exam.objects.create(
            exam_period=self.exam_period,
            subject_name="임박 과목",
            exam_date=self.tomorrow,
            speed_factor=1.0,
        )
        # later_exam: 시험일이 훨씬 뒤라서, near_exam의 시험일(tomorrow)에도 공부가 가능하다
        self.later_exam = Exam.objects.create(
            exam_period=self.exam_period,
            subject_name="다른 과목",
            exam_date=self.today + timedelta(days=5),
            speed_factor=1.0,
        )
        # quiet_exam: 아무 작업도 배치되지 않는 "순수 시험일" 테스트용
        self.quiet_date = self.today + timedelta(days=3)
        self.quiet_exam = Exam.objects.create(
            exam_period=self.exam_period,
            subject_name="조용한 시험 과목",
            exam_date=self.quiet_date,
            speed_factor=1.0,
        )

        self.task_core = StudyTask.objects.create(
            exam=self.later_exam, title="다른 과목 핵심 작업", importance="high", depth="core",
            task_type="concept", difficulty="normal", order=1,
            estimated_min_minutes=15, estimated_max_minutes=30, is_confirmed=True,
        )
        self.task_optional = StudyTask.objects.create(
            exam=self.later_exam, title="다른 과목 선택 작업", importance="low", depth="optional",
            task_type="practice", difficulty="normal", order=2,
            estimated_min_minutes=30, estimated_max_minutes=60, is_confirmed=True,
        )
        self.task_excluded = StudyTask.objects.create(
            exam=self.later_exam, title="제외될 작업", importance="low", depth="optional",
            task_type="practice", difficulty="normal", order=3,
            estimated_min_minutes=20, estimated_max_minutes=45, is_confirmed=True,
        )

        self.source_daily_plan = DailyPlan.objects.create(
            exam_period=self.exam_period, date=self.today,
            available_minutes=120, planned_minutes=135,
        )

        AvailableTime.objects.create(
            exam_period=self.exam_period, date=self.tomorrow, available_minutes=60,
        )
        AvailableTime.objects.create(
            exam_period=self.exam_period, date=self.day2, available_minutes=60,
        )

        # tomorrow에 "기존 계획"이 이미 10분 잡혀있는 상태 (before_minutes 검증용)
        existing_plan = DailyPlan.objects.create(
            exam_period=self.exam_period, date=self.tomorrow,
            available_minutes=60, planned_minutes=10,
        )
        DailyPlanItem.objects.create(
            daily_plan=existing_plan, study_task=self.task_core,
            planned_minutes=10, order=1,
        )

        self.recovery_plan = RecoveryPlan.objects.create(
            exam_period=self.exam_period,
            source_daily_plan=self.source_daily_plan,
            recovery_group_id=uuid.uuid4(),
            recovery_type=RecoveryType.MAINTAIN_VOLUME,
            status=RecoveryPlanStatus.PENDING,
        )
        # tomorrow(시험일)에 30분 재배치 -> before=10, added=30, after=40, available=60
        self.item_core = RecoveryPlanItem.objects.create(
            recovery_plan=self.recovery_plan, study_task=self.task_core,
            original_date=self.today, changed_date=self.tomorrow,
            action_type=RecoveryActionType.RESCHEDULE,
            remaining_minutes=30, reason="테스트",
        )
        # day2에 60분 재배치 -> before=0, added=60, after=60, available=60
        self.item_optional = RecoveryPlanItem.objects.create(
            recovery_plan=self.recovery_plan, study_task=self.task_optional,
            original_date=self.today, changed_date=self.day2,
            action_type=RecoveryActionType.RESCHEDULE,
            remaining_minutes=60, reason="테스트",
        )
        self.item_excluded = RecoveryPlanItem.objects.create(
            recovery_plan=self.recovery_plan, study_task=self.task_excluded,
            original_date=self.today, changed_date=None,
            action_type=RecoveryActionType.EXCLUDE,
            remaining_minutes=45, reason="테스트",
        )

    def test_exam_period_in_context(self):
        response = self._get(self.recovery_plan.id)
        self.assertEqual(response.context["exam_period"], self.exam_period)

    def _get(self, plan_id):
        return self.client.get(
            reverse("planner:recovery_preview", kwargs={"plan_id": plan_id})
        )

    def _days_by_date(self, response):
        return {d["date"]: d for d in response.context["preview"]["days"]}

    # ── 1. 정상 조회 ──────────────────────────────
    def test_returns_200_for_owned_pending_plan(self):
        response = self._get(self.recovery_plan.id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["plan"]["id"], self.recovery_plan.id)

    # ── 2. before/added/after 계산 정확성 ───────────
    def test_before_added_after_minutes_are_correct(self):
        response = self._get(self.recovery_plan.id)
        days = self._days_by_date(response)

        self.assertEqual(days[self.tomorrow]["before_minutes"], 10)
        self.assertEqual(days[self.tomorrow]["added_minutes"], 30)
        self.assertEqual(days[self.tomorrow]["after_minutes"], 40)  # 10 + 30

        self.assertEqual(days[self.day2]["before_minutes"], 0)
        self.assertEqual(days[self.day2]["added_minutes"], 60)
        self.assertEqual(days[self.day2]["after_minutes"], 60)  # 0 + 60

    # ── 3. before_pct/after_pct가 값에 비례하는지 (회귀 테스트) ──
    def test_pct_values_are_proportional_to_minutes(self):
        response = self._get(self.recovery_plan.id)
        days = self._days_by_date(response)

        tomorrow_day = days[self.tomorrow]
        # scale = max(10, 40, 60, 1) = 60
        self.assertAlmostEqual(tomorrow_day["before_pct"], 10 / 60 * 100, places=1)
        self.assertAlmostEqual(tomorrow_day["after_pct"], 40 / 60 * 100, places=1)
        self.assertAlmostEqual(tomorrow_day["limit_pct"], 100.0, places=1)

        day2_day = days[self.day2]
        # scale = max(0, 60, 60, 1) = 60
        self.assertAlmostEqual(day2_day["after_pct"], 100.0, places=1)

        # 30분짜리 날의 after_pct가 60분짜리 날보다 명확히 작아야 한다
        self.assertLess(tomorrow_day["after_pct"], day2_day["after_pct"])

    # ── 4. 시험일이어도 다른 과목 작업이 배치돼 있으면 숨기지 않는다 ──
    def test_is_exam_day_false_when_other_subject_scheduled(self):
        response = self._get(self.recovery_plan.id)
        days = self._days_by_date(response)

        # near_exam.exam_date == tomorrow 인데, 그 날 배치된 건 다른 과목(later_exam)의 작업
        self.assertFalse(days[self.tomorrow]["is_exam_day"])

    # ── 5. 유지/이동/제외 카운트 ─────────────────────
    def test_summary_counts_are_correct(self):
        response = self._get(self.recovery_plan.id)
        preview = response.context["preview"]

        self.assertEqual(preview["keep_count"], 2)  # RESCHEDULE 2개
        self.assertEqual(preview["keep_core_count"], 1)  # 그중 core 1개
        self.assertEqual(preview["move_count"], 2)  # 둘 다 original != changed
        self.assertEqual(preview["exclude_count"], 1)
        self.assertEqual(preview["exclude_minutes"], 45)

    # ── 6. compare_url이 올바른 그룹으로 연결되고, 방금 미리보기한 복구안을
    #      selected 쿼리파라미터로 넘겨서 비교 화면 복귀 시 선택이 유지되는지 ──
    def test_compare_url_points_back_to_same_group(self):
        response = self._get(self.recovery_plan.id)
        expected = reverse(
            "planner:recovery_compare",
            kwargs={"group_id": self.recovery_plan.recovery_group_id},
        ) + f"?selected={self.recovery_plan.id}"
        self.assertEqual(response.context["compare_url"], expected)

    # ── 7. 존재하지 않는 plan_id ─────────────────────
    def test_404_when_plan_does_not_exist(self):
        response = self._get(99999)
        self.assertEqual(response.status_code, 404)

    # ── 8. 다른 사용자의 복구안 ──────────────────────
    def test_404_when_owned_by_other_user(self):
        self.client.logout()
        self.client.login(username="recovery_preview_other@example.com", password="pass1234")

        response = self._get(self.recovery_plan.id)
        self.assertEqual(response.status_code, 404)

    # ── 9. PENDING이 아닌 복구안(이미 적용/폐기됨) ────
    def test_404_when_not_pending(self):
        self.recovery_plan.status = RecoveryPlanStatus.APPLIED
        self.recovery_plan.save(update_fields=["status"])

        response = self._get(self.recovery_plan.id)
        self.assertEqual(response.status_code, 404)

    # ── 10. 로그인 필요 ──────────────────────────────
    def test_requires_login(self):
        self.client.logout()
        response = self._get(self.recovery_plan.id)
        self.assertEqual(response.status_code, 302)

    # ── 11. DB를 수정하지 않는지 (미리보기 원칙) ──────
    def test_does_not_modify_database(self):
        before_daily_plan_count = DailyPlan.objects.count()
        before_item_count = DailyPlanItem.objects.count()
        before_recovery_status = self.recovery_plan.status

        self._get(self.recovery_plan.id)

        self.assertEqual(DailyPlan.objects.count(), before_daily_plan_count)
        self.assertEqual(DailyPlanItem.objects.count(), before_item_count)
        self.recovery_plan.refresh_from_db()
        self.assertEqual(self.recovery_plan.status, before_recovery_status)

# ── 12. 공부 계획이 전혀 없는 순수 시험일은 is_exam_day=True ──
    def test_is_exam_day_true_when_no_study_scheduled(self):
        response = self._get(self.recovery_plan.id)
        days = self._days_by_date(response)

        self.assertTrue(days[self.quiet_date]["is_exam_day"])
        self.assertEqual(days[self.quiet_date]["before_minutes"], 0)
        self.assertEqual(days[self.quiet_date]["after_minutes"], 0)

class RecoveryApplyViewTests(TestCase):
    """
    #81 recovery_apply View 테스트.

    apply_recovery_plan() 자체 로직은 ApplyRecoveryPlanTests가 이미 검증하므로,
    여기서는 View 레벨(권한, HTTP 메서드, 예외 → 메시지/리다이렉트 매핑)만 검증한다.
    """

    def setUp(self):
        from django.contrib.auth import get_user_model
        from exams.models import Exam, ExamPeriod, StudyTask

        User = get_user_model()
        self.user = User.objects.create_user(
            username="recovery_apply_tester",
            email="recovery_apply@example.com",
            password="pass1234",
        )
        self.other_user = User.objects.create_user(
            username="recovery_apply_other",
            email="recovery_apply_other@example.com",
            password="pass1234",
        )
        self.client.login(username="recovery_apply@example.com", password="pass1234")

        self.today = django_timezone.localdate()
        self.exam_period = ExamPeriod.objects.create(
            user=self.user,
            title="복구 적용 테스트 시험기간",
            start_date=self.today - timedelta(days=1),
            end_date=self.today + timedelta(days=10),
            status="active",
        )
        self.exam = Exam.objects.create(
            exam_period=self.exam_period,
            subject_name="테스트 과목",
            exam_date=self.today + timedelta(days=5),
        )
        self.protected_task = StudyTask.objects.create(
            exam=self.exam, title="보호 작업", importance="high", depth="core",
            task_type="concept", difficulty="normal", order=1,
            estimated_min_minutes=20, estimated_max_minutes=40, is_confirmed=True,
        )
        self.low_task = StudyTask.objects.create(
            exam=self.exam, title="제외 후보 작업", importance="low", depth="optional",
            task_type="concept", difficulty="normal", order=2,
            estimated_min_minutes=20, estimated_max_minutes=40, is_confirmed=True,
        )
        self.daily_plan = DailyPlan.objects.create(
            exam_period=self.exam_period, date=self.today,
            available_minutes=80, planned_minutes=80,
        )
        protected_item = DailyPlanItem.objects.create(
            daily_plan=self.daily_plan, study_task=self.protected_task,
            planned_minutes=40, order=1,
        )
        low_item = DailyPlanItem.objects.create(
            daily_plan=self.daily_plan, study_task=self.low_task,
            planned_minutes=40, order=2,
        )
        record_progress(daily_plan_item=protected_item, status="not_done", actual_minutes=0)
        record_progress(daily_plan_item=low_item, status="not_done", actual_minutes=0)

        AvailableTime.objects.create(
            exam_period=self.exam_period,
            date=self.today + timedelta(days=1),
            available_minutes=80,
        )

        result = finalize_daily_plan(self.daily_plan)
        self.maintain_volume = result["recovery_plans"]["maintain_volume"]
        self.core_focus = result["recovery_plans"]["core_focus"]
        self.assertIsNotNone(self.maintain_volume)
        self.assertIsNotNone(self.core_focus)

    def _apply(self, plan_id):
        return self.client.post(
            reverse("planner:recovery_apply", kwargs={"plan_id": plan_id})
        )

    # ── 1. 분량 유지형 정상 적용 ──────────────────────
    def test_apply_maintain_volume_success(self):
        response = self._apply(self.maintain_volume.id)

        self.assertRedirects(response, reverse("planner:dashboard"))
        self.maintain_volume.refresh_from_db()
        self.assertEqual(self.maintain_volume.status, RecoveryPlanStatus.APPLIED)

    # ── 2. 핵심 집중형 정상 적용 ──────────────────────
    def test_apply_core_focus_success(self):
        response = self._apply(self.core_focus.id)

        self.assertRedirects(response, reverse("planner:dashboard"))
        self.core_focus.refresh_from_db()
        self.assertEqual(self.core_focus.status, RecoveryPlanStatus.APPLIED)

    # ── 3. RESCHEDULE 항목이 미래 DailyPlanItem으로 생성되는지 ──
    def test_reschedule_items_create_future_daily_plan_items(self):
        reschedule_item = self.maintain_volume.items.get(
            study_task=self.protected_task, action_type="reschedule"
        )

        self._apply(self.maintain_volume.id)

        self.assertTrue(
            DailyPlanItem.objects.filter(
                study_task=self.protected_task,
                daily_plan__date=reschedule_item.changed_date,
            ).exists()
        )

    # ── 4. EXCLUDE 항목은 일정에 생성되지 않는지 ──────
    def test_excluded_items_not_created_in_schedule(self):
        self._apply(self.core_focus.id)

        excluded_task_ids = set(
            self.core_focus.items.filter(action_type="exclude")
            .values_list("study_task_id", flat=True)
        )
        self.assertTrue(excluded_task_ids)
        self.assertFalse(
            DailyPlanItem.objects.filter(
                study_task_id__in=excluded_task_ids,
                daily_plan__date__gt=self.today,
            ).exists()
        )

    # ── 5. 선택한 복구안 APPLIED로 변경 ───────────────
    def test_selected_plan_marked_applied(self):
        self._apply(self.maintain_volume.id)

        self.maintain_volume.refresh_from_db()
        self.assertEqual(self.maintain_volume.status, RecoveryPlanStatus.APPLIED)
        self.assertIsNotNone(self.maintain_volume.applied_at)

    # ── 6. 같은 그룹의 다른 복구안 DISCARDED로 변경 ───
    def test_sibling_plan_marked_discarded(self):
        self._apply(self.maintain_volume.id)

        self.core_focus.refresh_from_db()
        self.assertEqual(self.core_focus.status, RecoveryPlanStatus.DISCARDED)

    # ── 7. 동일 그룹 복구안 두 번 적용 거부 ───────────
    def test_applying_twice_in_same_group_rejected(self):
        self._apply(self.maintain_volume.id)

        response = self._apply(self.core_focus.id)

        self.assertRedirects(response, reverse("planner:dashboard"))
        self.core_focus.refresh_from_db()
        # 첫 적용(maintain_volume) 시점에 이미 DISCARDED로 바뀐 상태가 유지돼야 함
        self.assertEqual(self.core_focus.status, RecoveryPlanStatus.DISCARDED)

    # ── 8. 이미 처리된 복구안 적용 시 오류 메시지 ─────
    def test_already_processed_shows_error_message(self):
        self._apply(self.maintain_volume.id)

        response = self._apply(self.maintain_volume.id)

        messages_list = list(response.wsgi_request._messages)
        self.assertTrue(
            any("이미 처리된 복구안" in str(m) for m in messages_list)
        )

    # ── 9. stale 복구안 적용 시 전체 롤백 ─────────────
    def test_stale_plan_rolls_back_completely(self):
        AvailableTime.objects.filter(
            exam_period=self.exam_period, date=self.today + timedelta(days=1),
        ).delete()

        response = self._apply(self.maintain_volume.id)

        self.assertRedirects(
            response,
            reverse("planner:recovery_compare",
                    kwargs={"group_id": self.maintain_volume.recovery_group_id}),
        )
        self.maintain_volume.refresh_from_db()
        self.assertEqual(self.maintain_volume.status, RecoveryPlanStatus.PENDING)
        self.assertFalse(
            DailyPlanItem.objects.filter(
                study_task__in=[self.protected_task, self.low_task],
                daily_plan__date__gt=self.today,
            ).exists()
        )

    # ── 10. 유효하지 않은 remaining_minutes 적용 시 전체 롤백 ──
    def test_invalid_remaining_minutes_rolls_back(self):
        recovery_item = self.maintain_volume.items.filter(action_type="reschedule").first()
        recovery_item.remaining_minutes = 0
        recovery_item.save(update_fields=["remaining_minutes"])

        response = self._apply(self.maintain_volume.id)

        self.assertRedirects(
            response,
            reverse("planner:recovery_compare",
                    kwargs={"group_id": self.maintain_volume.recovery_group_id}),
        )
        self.maintain_volume.refresh_from_db()
        self.assertEqual(self.maintain_volume.status, RecoveryPlanStatus.PENDING)

    # ── 11. 다른 사용자 복구안 적용 시 404 ────────────
    def test_404_when_owned_by_other_user(self):
        self.client.logout()
        self.client.login(username="recovery_apply_other@example.com", password="pass1234")

        response = self._apply(self.maintain_volume.id)

        self.assertEqual(response.status_code, 404)

    # ── 12. GET 요청은 405 ────────────────────────────
    def test_get_method_not_allowed(self):
        response = self.client.get(
            reverse("planner:recovery_apply", kwargs={"plan_id": self.maintain_volume.id})
        )
        self.assertEqual(response.status_code, 405)

    # ── 13. 원본 과거 계획과 진행 기록 유지 ───────────
    def test_original_past_plan_and_progress_preserved(self):
        original_item_count = DailyPlanItem.objects.filter(
            daily_plan=self.daily_plan
        ).count()
        original_log_count = ProgressLog.objects.filter(
            daily_plan_item__daily_plan=self.daily_plan
        ).count()

        self._apply(self.maintain_volume.id)

        self.assertEqual(
            DailyPlanItem.objects.filter(daily_plan=self.daily_plan).count(),
            original_item_count,
        )
        self.assertEqual(
            ProgressLog.objects.filter(daily_plan_item__daily_plan=self.daily_plan).count(),
            original_log_count,
        )

    # ── 14. 로그인 필요 ────────────────────────────────
    def test_requires_login(self):
        self.client.logout()
        response = self._apply(self.maintain_volume.id)
        self.assertEqual(response.status_code, 302)

    # ── 15. 예상 못 한 예외도 500 대신 안내 메시지로 처리 ──
    @patch(
        "planner.views.apply_recovery_plan",
        side_effect=ValueError("예상 못 한 DB 오류"),
    )
    def test_unexpected_exception_redirects_instead_of_500(self, _mock):
        response = self._apply(self.maintain_volume.id)

        self.assertRedirects(
            response,
            reverse("planner:recovery_compare",
                    kwargs={"group_id": self.maintain_volume.recovery_group_id}),
        )
        messages_list = list(response.wsgi_request._messages)
        self.assertTrue(
            any("오류가 발생했습니다" in str(m) for m in messages_list)
        )

class DashboardContextTests(TestCase):
    """
    #102 dashboard() context(remaining_days/overall/subject_summary/progress) 테스트.
    """

    def setUp(self):
        from django.contrib.auth import get_user_model
        from exams.models import Exam, ExamPeriod, StudyTask

        User = get_user_model()
        self.user = User.objects.create_user(
            username="dashboard_ctx_tester",
            email="dashboard_ctx@example.com",
            password="pass1234",
        )
        self.client.login(username="dashboard_ctx@example.com", password="pass1234")

        self.today = django_timezone.localdate()
        self.exam_period = ExamPeriod.objects.create(
            user=self.user,
            title="대시보드 context 테스트 시험기간",
            start_date=self.today - timedelta(days=1),
            end_date=self.today + timedelta(days=10),
            status="active",
        )
        self.exam = Exam.objects.create(
            exam_period=self.exam_period,
            subject_name="테스트 과목",
            exam_date=self.today + timedelta(days=7),
            speed_factor=1.0,
        )

        # task_a: 완료 (concept/normal -> 20~40분, DONE이라 remaining 0)
        self.task_a = StudyTask.objects.create(
            exam=self.exam, title="완료 작업", importance="high", depth="basic",
            task_type="concept", difficulty="normal", order=1,
            estimated_min_minutes=20, estimated_max_minutes=40, is_confirmed=True,
        )
        # task_b: 오늘 절반 완료 (PARTIAL 50%) -> remaining min=10, max=20
        self.task_b = StudyTask.objects.create(
            exam=self.exam, title="일부완료 작업", importance="high", depth="basic",
            task_type="concept", difficulty="normal", order=2,
            estimated_min_minutes=20, estimated_max_minutes=40, is_confirmed=True,
        )
        # task_c: 손도 안 댐, CORE -> remaining min=20, max=40, core_left에 잡혀야 함
        self.task_c = StudyTask.objects.create(
            exam=self.exam, title="미착수 핵심 작업", importance="high", depth="core",
            task_type="concept", difficulty="normal", order=3,
            estimated_min_minutes=20, estimated_max_minutes=40, is_confirmed=True,
        )

        # 오늘 계획: task_a(완료), task_b(일부완료)
        self.today_plan = DailyPlan.objects.create(
            exam_period=self.exam_period, date=self.today,
            available_minutes=80, planned_minutes=80,
        )
        item_a = DailyPlanItem.objects.create(
            daily_plan=self.today_plan, study_task=self.task_a,
            planned_minutes=40, order=1,
        )
        item_b = DailyPlanItem.objects.create(
            daily_plan=self.today_plan, study_task=self.task_b,
            planned_minutes=40, order=2,
        )
        record_progress(daily_plan_item=item_a, status="done", actual_minutes=35)
        record_progress(
            daily_plan_item=item_b, status="partial",
            actual_minutes=20, completion_percent=50,
        )

        # 어제 계획: task_c(미착수, 기록 없음) -> total에는 잡히지만 today에는 안 잡혀야 함
        yesterday_plan = DailyPlan.objects.create(
            exam_period=self.exam_period, date=self.today - timedelta(days=1),
            available_minutes=40, planned_minutes=40,
        )
        DailyPlanItem.objects.create(
            daily_plan=yesterday_plan, study_task=self.task_c,
            planned_minutes=40, order=1,
        )

        # 미래 가용시간 3일치 (remaining_days, overall.available_minutes 계산용)
        for i in range(1, 4):
            AvailableTime.objects.create(
                exam_period=self.exam_period,
                date=self.today + timedelta(days=i),
                available_minutes=60,
            )

    def _get_dashboard(self):
        return self.client.get(reverse("planner:dashboard"))

    # ── 1. remaining_days ──────────────────────────
    def test_remaining_days_counts_future_available_dates(self):
        response = self._get_dashboard()
        self.assertEqual(response.context["remaining_days"], 3)

    # ── 2. overall - 필요시간 계산 (DONE/PARTIAL/미착수 각각 다르게) ──
    def test_overall_required_minutes_reflects_each_task_status(self):
        response = self._get_dashboard()
        overall = response.context["overall"]

        # task_a(DONE)=0,0 + task_b(PARTIAL 50%)=10,20 + task_c(미착수)=20,40
        self.assertEqual(overall["min_minutes"], 30)
        self.assertEqual(overall["max_minutes"], 60)

    # ── 3. overall - 가용시간 및 판정 ────────────────
    def test_overall_available_minutes_and_status(self):
        response = self._get_dashboard()
        overall = response.context["overall"]

        self.assertEqual(overall["available_minutes"], 180)  # 60분 x 3일
        self.assertEqual(overall["status"], POSSIBLE)
        self.assertEqual(overall["max_shortage_minutes"], 0)

    # ── 4. subject_summary ────────────────────────
    def test_subject_summary_excludes_done_task(self):
        response = self._get_dashboard()
        summary = response.context["subject_summary"]

        self.assertEqual(len(summary), 1)
        subject = summary[0]
        self.assertEqual(subject["subject_name"], "테스트 과목")
        # task_a(DONE)는 remaining 0이라 카운트에서 빠지고, task_b(20)+task_c(40)만 잡힘
        self.assertEqual(subject["remaining_task_count"], 2)
        self.assertEqual(subject["remaining_minutes"], 60)
        self.assertEqual(subject["d_day"], 7)

    # ── 5. progress - 오늘 vs 전체가 다른 범위를 봐야 함 ──
    def test_progress_today_differs_from_total(self):
        response = self._get_dashboard()
        progress = response.context["progress"]

        # 오늘: task_a(40, DONE 전액) + task_b(40, 50%=20) = done 60 / total 80
        self.assertEqual(progress["today_total_minutes"], 80)
        self.assertEqual(progress["today_done_minutes"], 60)
        self.assertEqual(progress["today_percent"], 75)

        # 전체: 오늘(80) + 어제 task_c(40, 기록없음=0 인정) = done 60 / total 120
        self.assertEqual(progress["total_minutes"], 120)
        self.assertEqual(progress["total_done_minutes"], 60)
        self.assertEqual(progress["total_percent"], 50)

    # ── 6. core_left - 기록 없는 CORE 작업만 카운트 ──
    def test_core_left_counts_untouched_core_tasks_only(self):
        response = self._get_dashboard()
        progress = response.context["progress"]

        # task_c만 CORE이고 기록이 없음 -> 1
        self.assertEqual(progress["core_left"], 1)

    # ── 7. 다른 사용자 시험기간은 안 보임 (기존 패턴 재확인) ──
    def test_other_user_does_not_see_this_exam_period(self):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        other = User.objects.create_user(
            username="dashboard_ctx_other",
            email="dashboard_ctx_other@example.com",
            password="pass1234",
        )
        self.client.logout()
        self.client.login(username="dashboard_ctx_other@example.com", password="pass1234")

        response = self._get_dashboard()
        self.assertIsNone(response.context["exam_period"])

# ── 8. core_left는 계획에 아직 안 들어간 CORE 작업도 포함 ──
    def test_core_left_includes_unscheduled_core_tasks(self):
        from exams.models import StudyTask

        # task_c는 이미 어제 계획에 배치돼 있음(setUp에서). 여기에 아직
        # 계획에 안 들어간 CORE 작업 2개를 추가로 만든다.
        StudyTask.objects.create(
            exam=self.exam, title="미배치 핵심 작업 1", importance="high", depth="core",
            task_type="concept", difficulty="normal", order=4,
            estimated_min_minutes=20, estimated_max_minutes=40, is_confirmed=True,
        )
        StudyTask.objects.create(
            exam=self.exam, title="미배치 핵심 작업 2", importance="high", depth="core",
            task_type="concept", difficulty="normal", order=5,
            estimated_min_minutes=20, estimated_max_minutes=40, is_confirmed=True,
        )

        response = self._get_dashboard()
        progress = response.context["progress"]

        # task_c(배치됨, 미착수) + 미배치 2개 = 총 3개
        self.assertEqual(progress["core_left"], 3)

    # ── 9. 이미 배치된 미래 작업이 available_minutes를 이중 차감하지 않는지 ──
    def test_overall_available_minutes_not_double_counted_by_scheduled_items(self):
        from exams.models import StudyTask

        future_task = StudyTask.objects.create(
            exam=self.exam, title="이미 배치된 작업", importance="high", depth="basic",
            task_type="concept", difficulty="normal", order=10,
            estimated_min_minutes=60, estimated_max_minutes=60, is_confirmed=True,
        )
        tomorrow = self.today + timedelta(days=1)
        future_plan = DailyPlan.objects.filter(
            exam_period=self.exam_period, date=tomorrow
        ).first()
        if future_plan is None:
            future_plan = DailyPlan.objects.create(
                exam_period=self.exam_period, date=tomorrow,
                available_minutes=60, planned_minutes=0,
            )
        DailyPlanItem.objects.create(
            daily_plan=future_plan, study_task=future_task,
            planned_minutes=60, order=99,
        )

        response = self._get_dashboard()
        overall = response.context["overall"]

        # available_minutes는 occupied 차감 없이 AvailableTime 원본 총량(180)이어야 한다
        self.assertEqual(overall["available_minutes"], 180)

    def test_progress_total_does_not_double_count_recovery_item(self):
        """
        같은 StudyTask의 남은 분량이 복구 후 새 DailyPlanItem으로 생겨도
        전체 계획량에 중복 합산되지 않아야 한다.
        """
        from planner.models import DailyPlan, DailyPlanItem

        recovery_plan = DailyPlan.objects.create(
            exam_period=self.exam_period,
            date=self.today + timedelta(days=1),
            available_minutes=60,
            planned_minutes=20,
        )

        DailyPlanItem.objects.create(
            daily_plan=recovery_plan,
            study_task=self.task_a,
            planned_minutes=20,
            order=1,
        )

        response = self._get_dashboard()
        progress = response.context["progress"]

        # 기존 총량:
        # task_a 40 + task_b 40 + task_c 40 = 120
        #
        # task_a의 복구 아이템 20분이 추가되어도
        # 같은 study_task이므로 총량은 140이 아니라 120이어야 한다.
        self.assertEqual(progress["total_minutes"], 120)
        self.assertEqual(progress["total_done_minutes"], 60)
        self.assertEqual(progress["total_percent"], 50)


    def test_progress_done_sums_original_and_recovery_items(self):
        """
        원본 아이템의 PARTIAL 진행분과 복구 아이템의 완료분은
        같은 StudyTask의 완료량으로 합산하되 총량은 중복되지 않아야 한다.
        """
        from planner.models import DailyPlan, DailyPlanItem, ProgressLog

        recovery_plan = DailyPlan.objects.create(
            exam_period=self.exam_period,
            date=self.today + timedelta(days=1),
            available_minutes=60,
            planned_minutes=20,
        )

        recovery_item = DailyPlanItem.objects.create(
            daily_plan=recovery_plan,
            study_task=self.task_b,
            planned_minutes=20,
            order=1,
        )

        ProgressLog.objects.create(
            daily_plan_item=recovery_item,
            progress_status="done",
            actual_minutes=20,
            completion_percent=100,
        )

        response = self._get_dashboard()
        progress = response.context["progress"]

        # 기존 완료량:
        # task_a DONE = 40
        # task_b PARTIAL 50% = 20
        #
        # 복구된 task_b 20분 DONE = +20
        # 따라서 80 / 120 = 66.67% -> round() = 67
        self.assertEqual(progress["total_minutes"], 120)
        self.assertEqual(progress["total_done_minutes"], 80)
        self.assertEqual(progress["total_percent"], 67)


class CalendarServiceYearMonthValidationTests(TestCase):
    """
    build_calendar_context() 자체가 year/month를 검증하는지 확인한다 (리뷰 반영).
    View(planner/views.py)에 방어 코드가 없어도(또는 나중에 또 빠지더라도),
    이 서비스 함수를 직접 호출하는 어떤 경로에서든 최소한의 안전장치가
    되도록 함수 내부에서도 검증한다.
    """

    def setUp(self):
        from django.contrib.auth import get_user_model
        from exams.models import ExamPeriod

        User = get_user_model()
        self.user = User.objects.create_user(
            username="calendar_service_tester", email="calendar_service_tester@example.com",
            password="pass1234",
        )
        self.today = django_timezone.localdate()
        self.period = ExamPeriod.objects.create(
            user=self.user, title="검증 테스트 시험기간",
            start_date=self.today - timedelta(days=3),
            end_date=self.today + timedelta(days=10),
            status="active",
        )

    def test_month_13_falls_back_to_today(self):
        from planner.services.calendar import build_calendar_context

        data = build_calendar_context(self.period, self.today.year, 13)

        self.assertEqual(data["year"], self.today.year)
        self.assertEqual(data["month"], self.today.month)
        self.assertEqual(len(data["weeks"]), 6)

    def test_month_zero_falls_back_to_today(self):
        from planner.services.calendar import build_calendar_context

        data = build_calendar_context(self.period, self.today.year, 0)

        self.assertEqual(data["year"], self.today.year)
        self.assertEqual(data["month"], self.today.month)

    def test_year_out_of_datetime_range_falls_back_to_today(self):
        from planner.services.calendar import build_calendar_context

        data = build_calendar_context(self.period, 10000, 8)

        self.assertEqual(data["year"], self.today.year)
        self.assertEqual(data["month"], self.today.month)

    def test_non_integer_values_fall_back_to_today(self):
        from planner.services.calendar import build_calendar_context

        data = build_calendar_context(self.period, "abc", "xyz")

        self.assertEqual(data["year"], self.today.year)
        self.assertEqual(data["month"], self.today.month)

    def test_year_9999_december_boundary_does_not_crash(self):
        """
        year=9999, month=12는 1~12/1~9999 범위 안이라 '형식상' 유효하지만,
        6주 격자를 채우다 보면 다음 해(10000년) 날짜가 필요해져서 그대로
        두면 ValueError가 난다. 이 경계 케이스도 오늘 날짜로 안전하게
        대체되어야 한다.
        """
        from planner.services.calendar import build_calendar_context

        data = build_calendar_context(self.period, 9999, 12)

        self.assertEqual(data["year"], self.today.year)
        self.assertEqual(data["month"], self.today.month)
        self.assertEqual(len(data["weeks"]), 6)

    def test_year_1_january_boundary_does_not_crash(self):
        """
        year=1, month=1은 하한 경계값이다 (datetime.MINYEAR=1). 1월 1일이
        속한 주(일요일 시작)를 채우려면 그 전 며칠(0년 12월)이 필요한데,
        datetime은 0년을 표현할 수 없어 ValueError("year 0 is out of range")가
        난다. 상한 경계(9999/12)뿐 아니라 이 하한 경계도 오늘 날짜로 안전하게
        대체되어야 한다.
        """
        from planner.services.calendar import build_calendar_context

        data = build_calendar_context(self.period, 1, 1)

        self.assertEqual(data["year"], self.today.year)
        self.assertEqual(data["month"], self.today.month)
        self.assertEqual(len(data["weeks"]), 6)

    def test_valid_year_month_is_not_altered(self):
        from planner.services.calendar import build_calendar_context

        data = build_calendar_context(self.period, 2026, 8)

        self.assertEqual(data["year"], 2026)
        self.assertEqual(data["month"], 8)

class RecoveryRetryTests(TestCase):
    """
    #141 복구안 재생성(needs_recovery_retry / retry_recovery_generation / recovery_retry View) 테스트.
    """

    def setUp(self):
        from django.contrib.auth import get_user_model
        from exams.models import Exam, ExamPeriod, StudyTask

        User = get_user_model()
        self.user = User.objects.create_user(
            username="recovery_retry_tester",
            email="recovery_retry@example.com",
            password="pass1234",
        )
        self.other_user = User.objects.create_user(
            username="recovery_retry_other",
            email="recovery_retry_other@example.com",
            password="pass1234",
        )
        self.client.login(username="recovery_retry@example.com", password="pass1234")

        self.today = django_timezone.localdate()
        self.exam_period = ExamPeriod.objects.create(
            user=self.user,
            title="복구 재생성 테스트용 시험기간",
            start_date=self.today - timedelta(days=1),
            end_date=self.today + timedelta(days=10),
            status="active",
        )
        self.exam = Exam.objects.create(
            exam_period=self.exam_period,
            subject_name="테스트 과목",
            exam_date=self.today + timedelta(days=7),
        )
        # importance=high, depth=core -> 핵심집중형에서도 제외 대상이 안 됨
        # -> 가용시간이 없으면 두 안 다 실패하기 딱 좋은 조건
        self.task = StudyTask.objects.create(
            exam=self.exam, title="미완료 작업", importance="high", depth="core",
            task_type="concept", difficulty="normal", order=1,
            estimated_min_minutes=60, estimated_max_minutes=60, is_confirmed=True,
        )

        self.daily_plan = DailyPlan.objects.create(
            exam_period=self.exam_period, date=self.today,
            available_minutes=60, planned_minutes=60,
        )
        self.item = DailyPlanItem.objects.create(
            daily_plan=self.daily_plan, study_task=self.task,
            planned_minutes=60, order=1,
        )
        record_progress(daily_plan_item=self.item, status="not_done", actual_minutes=0)

        self.daily_plan.finalized_at = django_timezone.now()
        self.daily_plan.save(update_fields=["finalized_at"])
        # 미래 가용시간을 일부러 안 만듦 -> 복구안 생성 실패 상태를 재현

    def _retry(self):
        return self.client.post(
            reverse("planner:recovery_retry", kwargs={"daily_plan_id": self.daily_plan.id})
        )

    # ── 1. needs_recovery_retry 판별 ──────────────────
    def test_needs_retry_true_when_finalized_unfinished_no_recovery_plan(self):
        self.assertTrue(needs_recovery_retry(self.daily_plan))

    def test_needs_retry_false_when_not_finalized(self):
        self.daily_plan.finalized_at = None
        self.daily_plan.save(update_fields=["finalized_at"])
        self.assertFalse(needs_recovery_retry(self.daily_plan))

    def test_needs_retry_false_when_no_unfinished_items(self):
        self.item.progress_log.progress_status = "done"
        self.item.progress_log.save(update_fields=["progress_status"])
        self.assertFalse(needs_recovery_retry(self.daily_plan))

    def test_needs_retry_false_when_recovery_plan_already_exists(self):
        RecoveryPlan.objects.create(
            exam_period=self.exam_period, source_daily_plan=self.daily_plan,
            recovery_group_id=uuid.uuid4(), recovery_type="maintain_volume",
        )
        self.assertFalse(needs_recovery_retry(self.daily_plan))

    # ── 2. retry_recovery_generation 서비스 함수 ──────
    def test_retry_raises_when_not_needed(self):
        self.item.progress_log.progress_status = "done"
        self.item.progress_log.save(update_fields=["progress_status"])

        with self.assertRaises(RecoveryRetryNotNeededError):
            retry_recovery_generation(self.daily_plan)

    def test_retry_succeeds_after_available_time_increased(self):
        AvailableTime.objects.create(
            exam_period=self.exam_period,
            date=self.today + timedelta(days=1),
            available_minutes=100,
        )

        result = retry_recovery_generation(self.daily_plan)

        self.assertIsNotNone(result["maintain_volume"])
        self.assertEqual(
            RecoveryPlan.objects.filter(source_daily_plan=self.daily_plan).count(), 1
        )

    def test_retry_does_not_touch_finalized_at_or_progress_log(self):
        original_finalized_at = self.daily_plan.finalized_at
        AvailableTime.objects.create(
            exam_period=self.exam_period,
            date=self.today + timedelta(days=1),
            available_minutes=100,
        )

        retry_recovery_generation(self.daily_plan)

        self.daily_plan.refresh_from_db()
        self.item.progress_log.refresh_from_db()
        self.assertEqual(self.daily_plan.finalized_at, original_finalized_at)
        self.assertEqual(self.item.progress_log.progress_status, "not_done")

    # ── 3. View ────────────────────────────────────
    def test_view_redirects_to_dashboard_when_not_needed(self):
        self.item.progress_log.progress_status = "done"
        self.item.progress_log.save(update_fields=["progress_status"])

        response = self._retry()
        self.assertRedirects(response, reverse("planner:dashboard"))

    def test_view_still_fails_shows_error_and_redirects_dashboard(self):
        response = self._retry()
        self.assertRedirects(response, reverse("planner:dashboard"))
        self.assertFalse(
            RecoveryPlan.objects.filter(source_daily_plan=self.daily_plan).exists()
        )

    def test_view_success_redirects_to_recovery_compare(self):
        AvailableTime.objects.create(
            exam_period=self.exam_period,
            date=self.today + timedelta(days=1),
            available_minutes=100,
        )

        response = self._retry()

        plan = RecoveryPlan.objects.get(source_daily_plan=self.daily_plan)
        self.assertRedirects(
            response,
            reverse("planner:recovery_compare", kwargs={"group_id": plan.recovery_group_id}),
        )

    def test_view_404_for_other_user(self):
        self.client.logout()
        self.client.login(username="recovery_retry_other@example.com", password="pass1234")

        response = self._retry()
        self.assertEqual(response.status_code, 404)

    def test_view_requires_post(self):
        response = self.client.get(
            reverse("planner:recovery_retry", kwargs={"daily_plan_id": self.daily_plan.id})
        )
        self.assertEqual(response.status_code, 405)

    def test_concurrent_retry_does_not_create_duplicate_recovery_plans(self):
        AvailableTime.objects.create(
            exam_period=self.exam_period,
            date=self.today + timedelta(days=1),
            available_minutes=100,
        )

        retry_recovery_generation(self.daily_plan)
        with self.assertRaises(RecoveryRetryNotNeededError):
            retry_recovery_generation(self.daily_plan)

        self.assertEqual(
            RecoveryPlan.objects.filter(source_daily_plan=self.daily_plan)
            .values('recovery_group_id').distinct().count(),
            1,
        )

class RecoveryOrderPreservationTests(TestCase):
    """
    #162: 복구 재배치 시 같은 과목 후속 작업의 학습 순서가 보존되는지 검증한다.
    """

    def setUp(self):
        from django.contrib.auth import get_user_model
        from exams.models import Exam, ExamPeriod, StudyTask

        User = get_user_model()
        self.user = User.objects.create_user(
            username="order_tester",
            email="order_tester@example.com",
            password="pass1234",
        )
        self.client.login(username="order_tester@example.com", password="pass1234")

        self.today = django_timezone.localdate()
        self.exam_period = ExamPeriod.objects.create(
            user=self.user,
            title="순서 보존 테스트용 시험기간",
            start_date=self.today - timedelta(days=1),
            end_date=self.today + timedelta(days=20),
            status="active",
        )
        self.exam = Exam.objects.create(
            exam_period=self.exam_period,
            subject_name="운영체제",
            exam_date=self.today + timedelta(days=15),
            speed_factor=1.0,
        )
        self.other_exam = Exam.objects.create(
            exam_period=self.exam_period,
            subject_name="자료구조",
            exam_date=self.today + timedelta(days=15),
            speed_factor=1.0,
        )

        self.task_1_2 = StudyTask.objects.create(
            exam=self.exam, title="1-2 프로세스", importance="high", depth="core",
            task_type="concept", difficulty="normal", order=2,
            estimated_min_minutes=30, estimated_max_minutes=30, is_confirmed=True,
        )
        self.task_2_1 = StudyTask.objects.create(
            exam=self.exam, title="2-1 스레드", importance="high", depth="core",
            task_type="concept", difficulty="normal", order=3,
            estimated_min_minutes=30, estimated_max_minutes=30, is_confirmed=True,
        )
        self.task_2_2 = StudyTask.objects.create(
            exam=self.exam, title="2-2 동기화", importance="high", depth="core",
            task_type="concept", difficulty="normal", order=4,
            estimated_min_minutes=30, estimated_max_minutes=30, is_confirmed=True,
        )
        # 다른 과목 작업 (순서 제약이 섞이면 안 됨)
        self.task_ds_1 = StudyTask.objects.create(
            exam=self.other_exam, title="자료구조 1단원", importance="high", depth="core",
            task_type="concept", difficulty="normal", order=1,
            estimated_min_minutes=30, estimated_max_minutes=30, is_confirmed=True,
        )

        self.source_plan = DailyPlan.objects.create(
            exam_period=self.exam_period, date=self.today - timedelta(days=1),
            available_minutes=30, planned_minutes=30,
        )
        self.item_1_2 = DailyPlanItem.objects.create(
            daily_plan=self.source_plan, study_task=self.task_1_2,
            planned_minutes=30, order=1,
        )
        record_progress(daily_plan_item=self.item_1_2, status="not_done", actual_minutes=0)
        self.source_plan.finalized_at = django_timezone.now()
        self.source_plan.save(update_fields=["finalized_at"])

        self.future_plan_1 = DailyPlan.objects.create(
            exam_period=self.exam_period, date=self.today,
            available_minutes=60, planned_minutes=60,
        )
        self.item_2_1 = DailyPlanItem.objects.create(
            daily_plan=self.future_plan_1, study_task=self.task_2_1,
            planned_minutes=30, order=1,
        )
        self.item_ds_1 = DailyPlanItem.objects.create(
            daily_plan=self.future_plan_1, study_task=self.task_ds_1,
            planned_minutes=30, order=2,
        )

        self.future_plan_2 = DailyPlan.objects.create(
            exam_period=self.exam_period, date=self.today + timedelta(days=1),
            available_minutes=30, planned_minutes=30,
        )
        self.item_2_2 = DailyPlanItem.objects.create(
            daily_plan=self.future_plan_2, study_task=self.task_2_2,
            planned_minutes=30, order=1,
        )

        for i in range(2, 15):
            AvailableTime.objects.create(
                exam_period=self.exam_period,
                date=self.today + timedelta(days=i),
                available_minutes=60,
            )

    def test_carry_along_uses_min_order_so_all_following_tasks_included(self):
        """
        같은 과목에 미완료 작업이 여러 개일 때도(여기선 1개뿐이지만),
        가장 앞선 미완료 order를 기준으로 그 뒤 모든 미진행 작업(2-1, 2-2)이
        빠짐없이 carry-along으로 잡혀야 한다.
        """
        carry_along = _get_carry_along_items(self.source_plan, [self.item_1_2])
        carry_along_tasks = {item.study_task for item in carry_along}
        self.assertEqual(carry_along_tasks, {self.task_2_1, self.task_2_2})

    def test_maintain_volume_preserves_order_within_exam(self):
        """1-2 -> 2-1 -> 2-2 순서로, 날짜가 뒤로 갈수록만 배치돼야 한다."""
        result = generate_recovery_options(self.source_plan, [self.item_1_2])
        maintain_volume = result['maintain_volume']
        self.assertIsNotNone(maintain_volume, result['maintain_volume_failure_reason'])

        d1 = maintain_volume.items.get(study_task=self.task_1_2).changed_date
        d2 = maintain_volume.items.get(study_task=self.task_2_1).changed_date
        d3 = maintain_volume.items.get(study_task=self.task_2_2).changed_date
        self.assertLessEqual(d1, d2)
        self.assertLessEqual(d2, d3)

    def test_other_exam_not_constrained_by_carry_along(self):
        """
        운영체제 순서 보존이 자료구조 작업 배치 날짜에 영향을 주면 안 된다
        (자료구조는 원래 자리 그대로 유지되거나, 자기 시험일 안에서 자유롭게).
        """
        result = generate_recovery_options(self.source_plan, [self.item_1_2])
        maintain_volume = result['maintain_volume']
        self.assertIsNotNone(maintain_volume)

        # 자료구조는 carry-along 대상이 아니었으므로 RecoveryPlanItem에
        # 아예 없어야 한다 (건드리지 않았어야 함).
        self.assertFalse(
            maintain_volume.items.filter(study_task=self.task_ds_1).exists()
        )
        # 원래 자리에 그대로 남아있어야 한다.
        self.assertTrue(
            DailyPlanItem.objects.filter(
                daily_plan=self.future_plan_1, study_task=self.task_ds_1
            ).exists()
        )

    def test_carry_along_frees_up_original_capacity(self):
        """
        2-1, 2-2가 재배치되면서 원래 자리(future_plan_1, future_plan_2)의
        점유량이 용량 계산에서 빠져야, 재배치가 가용시간 부족 없이 성공한다.
        (원래 자리 점유량을 안 빼면 이 테스트가 실패로 나와야 정상이다.)
        """
        result = generate_recovery_options(self.source_plan, [self.item_1_2])
        self.assertIsNotNone(result['maintain_volume'])

    def test_apply_moves_existing_item_without_duplication(self):
        """
        적용 후 2-1, 2-2는 원래 자리에서 사라지고 새 자리에 하나씩만
        존재해야 한다 (삭제+재생성이 아니라 이동이므로 pk도 그대로 유지).
        """
        result = generate_recovery_options(self.source_plan, [self.item_1_2])
        maintain_volume = result['maintain_volume']

        original_item_2_1_id = self.item_2_1.id

        apply_recovery_plan(maintain_volume)

        task_2_1_items = DailyPlanItem.objects.filter(study_task=self.task_2_1)
        self.assertEqual(task_2_1_items.count(), 1)
        self.assertEqual(task_2_1_items.first().id, original_item_2_1_id)

        self.assertFalse(
            DailyPlanItem.objects.filter(
                daily_plan=self.future_plan_1, study_task=self.task_2_1
            ).exists()
        )

        # 원래 자리(future_plan_1)의 planned_minutes도 다시 계산됐어야 한다
        # (2-1이 빠졌으니 자료구조 30분만 남아야 함).
        self.future_plan_1.refresh_from_db()
        self.assertEqual(self.future_plan_1.planned_minutes, 30)

    def test_core_focus_excludes_middle_task_and_preserves_remaining_order(self):
        """
        핵심집중형에서 2-1이 제외되면, 남은 1-2 -> 2-2 순서만 유지되면
        된다 (제외된 작업은 순서 체인에서 완전히 빠짐).
        """
        # 2-1을 제외 대상이 되도록 importance/depth를 낮춘다.
        self.task_2_1.importance = "low"
        self.task_2_1.depth = "optional"
        self.task_2_1.save(update_fields=["importance", "depth"])

        result = generate_recovery_options(self.source_plan, [self.item_1_2])
        core_focus = result['core_focus']
        self.assertIsNotNone(core_focus, result['core_focus_failure_reason'])

        excluded_tasks = {
            item.study_task for item in
            core_focus.items.filter(action_type=RecoveryActionType.EXCLUDE)
        }
        self.assertIn(self.task_2_1, excluded_tasks)

        d1 = core_focus.items.get(study_task=self.task_1_2, action_type=RecoveryActionType.RESCHEDULE).changed_date
        d3 = core_focus.items.get(study_task=self.task_2_2, action_type=RecoveryActionType.RESCHEDULE).changed_date
        self.assertLessEqual(d1, d3)

    def test_apply_does_not_falsely_reject_carry_along_as_stale(self):
        """
        생성 시 carry-along의 원래 점유시간을 빼주고 계산했는데,
        적용 시 그 원래 점유시간을 다시 세서 stale로 거부되면 안 된다
        (이중 차감 버그 재발 방지).
        """
        result = generate_recovery_options(self.source_plan, [self.item_1_2])
        maintain_volume = result['maintain_volume']
        self.assertIsNotNone(maintain_volume)

        # 예외 없이 적용까지 성공해야 한다.
        apply_recovery_plan(maintain_volume)

        self.assertEqual(
            DailyPlanItem.objects.filter(study_task=self.task_2_1).count(), 1
        )

    def test_order_preserved_allocator_prefers_earliest_not_best_fit(self):
        """
        D1(더 이른 날짜, 여유 넉넉함), D2(더 늦은 날짜, 딱 한 작업만 들어갈
        만큼만 여유)에서, order가 앞선 작업이 '가장 딱 맞는'(best-fit) D2가
        아니라 '가장 이른'(earliest-fit) D1에 배치돼야, 뒤따르는 작업들이
        D1/D2에 나눠 들어갈 자리가 남는다.
        """
        from planner.services.time_estimator import estimate_task_minutes

        # 실제 재계산되는 필요시간을 직접 가져와서 시나리오를 정확히 맞춘다
        # (StudyTask.estimated_max_minutes를 그대로 쓰는 게 아니라
        # task_type/difficulty/speed_factor 기준으로 다시 계산되므로).
        _min, per_task_minutes = estimate_task_minutes(
            self.task_1_2.task_type, self.task_1_2.difficulty, self.exam.speed_factor
        )

        d1 = self.today + timedelta(days=1)
        d2 = self.today + timedelta(days=2)

        AvailableTime.objects.filter(
            exam_period=self.exam_period, date__gt=d2,
        ).update(available_minutes=0)
        # D1: 두 작업이 들어가고도 남을 만큼 넉넉하게
        AvailableTime.objects.update_or_create(
            exam_period=self.exam_period, date=d1,
            defaults={'available_minutes': per_task_minutes * 3},
        )
        # D2: 딱 한 작업만 들어갈 만큼만 (best-fit이었다면 여기 먼저 꽂힘)
        AvailableTime.objects.update_or_create(
            exam_period=self.exam_period, date=d2,
            defaults={'available_minutes': per_task_minutes},
        )

        result = generate_recovery_options(self.source_plan, [self.item_1_2])
        maintain_volume = result['maintain_volume']
        self.assertIsNotNone(maintain_volume, result.get('maintain_volume_failure_reason'))

        d1_2 = maintain_volume.items.get(study_task=self.task_1_2).changed_date
        d2_1 = maintain_volume.items.get(study_task=self.task_2_1).changed_date
        d2_2 = maintain_volume.items.get(study_task=self.task_2_2).changed_date

        # best-fit이었다면 1-2가 D2(더 딱 맞음)에 먼저 들어가서 cursor가
        # D2로 이동, 이후 2-1/2-2가 배치될 자리가 없어 전체 실패했을 것.
        self.assertEqual(d1_2, d1)
        self.assertLessEqual(d2_1, d2_2)

    def test_core_focus_exclusion_actually_removes_carry_along_from_schedule(self):
        """
        핵심집중형에서 carry-along 작업(2-1)이 EXCLUDE되면, 복구안에만
        표시되는 게 아니라 실제 DailyPlanItem도 apply 이후 사라져야 한다.
        """
        self.task_2_1.importance = "low"
        self.task_2_1.depth = "optional"
        self.task_2_1.save(update_fields=["importance", "depth"])

        result = generate_recovery_options(self.source_plan, [self.item_1_2])
        core_focus = result['core_focus']
        self.assertIsNotNone(core_focus, result['core_focus_failure_reason'])

        excluded_item = core_focus.items.get(
            study_task=self.task_2_1, action_type=RecoveryActionType.EXCLUDE
        )
        self.assertEqual(excluded_item.source_daily_plan_item_id, self.item_2_1.id)

        original_id = self.item_2_1.id
        apply_recovery_plan(core_focus)

        self.assertFalse(
            DailyPlanItem.objects.filter(id=original_id).exists()
        )
        self.future_plan_1.refresh_from_db()
        # future_plan_1에는 원래 자료구조(30) + 2-1(30) = 60이었는데,
        # 2-1이 제외됐으니 30만 남아야 한다.
        self.assertEqual(self.future_plan_1.planned_minutes, 30)

    def test_apply_rejects_when_carry_along_source_deleted(self):
        """
        복구안 생성 후, 적용 전에 원본(2-1) DailyPlanItem이 삭제되면
        (예: 다른 복구안이 먼저 적용되면서), is_carry_along=True인데
        source_daily_plan_item이 SET_NULL로 None이 된 상태다.

        이 경우 "원래 실패 작업이었던 것"처럼 조용히 새로 만들면 안 되고
        RecoveryPlanStaleError로 거부해야 한다.
        """
        result = generate_recovery_options(self.source_plan, [self.item_1_2])
        maintain_volume = result['maintain_volume']
        self.assertIsNotNone(maintain_volume)

        # 원본 2-1을 미리 삭제 -> source_daily_plan_item이 SET_NULL로 None이 됨
        self.item_2_1.delete()

        with self.assertRaises(RecoveryPlanStaleError):
            apply_recovery_plan(maintain_volume)

        # 신규 DailyPlanItem이 생성되지 않았어야 한다.
        self.assertFalse(
            DailyPlanItem.objects.filter(
                study_task=self.task_2_1,
                daily_plan__date__gt=self.source_plan.date,
            ).exists()
        )

    def test_core_focus_exclusion_frees_capacity_for_actual_use(self):
        """
        핵심집중형에서 2-1이 EXCLUDE되면, 2-1이 원래 차지하던 시간이
        재배치에 실제로 사용될 수 있어야 한다. (용량을 EXCLUDE 시점에
        반환하지 않으면, 충분히 가능한 배치도 가용시간 부족으로
        실패한다.)
        """
        from planner.services.time_estimator import estimate_task_minutes

        _min, per_task_minutes = estimate_task_minutes(
            self.task_1_2.task_type, self.task_1_2.difficulty, self.exam.speed_factor
        )
        d1 = self.today + timedelta(days=1)
        d2 = self.today + timedelta(days=2)

        # d1에는 2-1이 이미 차지 중인 딱 그만큼만, d2도 2-2가 차지 중인
        # 딱 그만큼만 가용시간을 준다. 즉 2-1이 EXCLUDE로 빠져야만
        # 1-2가 d1에 들어갈 여유가 생긴다.
        AvailableTime.objects.filter(
            exam_period=self.exam_period, date__gt=d2,
        ).update(available_minutes=0)
        AvailableTime.objects.update_or_create(
            exam_period=self.exam_period, date=d1,
            defaults={'available_minutes': per_task_minutes},  # 2-1 자리만큼만
        )
        AvailableTime.objects.update_or_create(
            exam_period=self.exam_period, date=d2,
            defaults={'available_minutes': per_task_minutes},  # 2-2 자리만큼만
        )
        # future_plan_1/2의 available_minutes도 위와 동기화 (occupied 계산에
        # 영향 없지만 일관성 유지 차원).

        self.task_2_1.importance = "low"
        self.task_2_1.depth = "optional"
        self.task_2_1.save(update_fields=["importance", "depth"])

        result = generate_recovery_options(self.source_plan, [self.item_1_2])
        core_focus = result['core_focus']
        self.assertIsNotNone(core_focus, result['core_focus_failure_reason'])

        # 2-1이 실제로 EXCLUDE됐는지 확인
        self.assertTrue(
            core_focus.items.filter(
                study_task=self.task_2_1, action_type=RecoveryActionType.EXCLUDE
            ).exists()
        )
        # 1-2가 배치될 자리를 확보했는지 확인 (2-1이 빠진 d1에 들어감)
        item_1_2_recovery = core_focus.items.get(
            study_task=self.task_1_2, action_type=RecoveryActionType.RESCHEDULE
        )
        self.assertEqual(item_1_2_recovery.changed_date, d1)

    def test_apply_rejects_when_source_already_moved_by_other_recovery(self):
        """
        같은 미래 작업(2-1)을 참조하는 복구안이 생성된 후, 다른 경로로
        2-1이 이미 다른 날짜로 이동됐다면(다른 복구안 적용 등), 이
        복구안을 나중에 적용하려 하면 "원본이 다른 곳으로 이미 이동됨"
        으로 거부돼야 한다. 그렇지 않으면 이미 이동된 작업을 또
        엉뚱하게 옮겨버릴 수 있다.
        """
        result = generate_recovery_options(self.source_plan, [self.item_1_2])
        maintain_volume = result['maintain_volume']
        self.assertIsNotNone(maintain_volume)

        # 다른 경로로 2-1이 이미 다른 날짜로 옮겨졌다고 가정
        # (원본 daily_plan.date를 바꿔서 재현).
        moved_date = self.today + timedelta(days=10)
        moved_plan, _ = DailyPlan.objects.get_or_create(
            exam_period=self.exam_period, date=moved_date,
            defaults={'available_minutes': 60, 'planned_minutes': 0},
        )
        self.item_2_1.daily_plan = moved_plan
        self.item_2_1.save(update_fields=['daily_plan'])

        with self.assertRaises(RecoveryPlanStaleError):
            apply_recovery_plan(maintain_volume)

    def test_apply_rejects_core_focus_exclude_when_source_deleted(self):
        """
        핵심집중형에서 carry-along(2-1)이 EXCLUDE된 뒤, 적용 전에
        원본이 삭제되면(SET_NULL) is_carry_along=True인데 source가
        None인 상태가 된다. 이 경우도 RESCHEDULE과 동일하게
        RecoveryPlanStaleError로 거부돼야 하고, 원본이 조용히
        무시된 채 나머지만 적용되면 안 된다.
        """
        self.task_2_1.importance = "low"
        self.task_2_1.depth = "optional"
        self.task_2_1.save(update_fields=["importance", "depth"])

        result = generate_recovery_options(self.source_plan, [self.item_1_2])
        core_focus = result['core_focus']
        self.assertIsNotNone(core_focus, result['core_focus_failure_reason'])

        excluded_item = core_focus.items.get(
            study_task=self.task_2_1, action_type=RecoveryActionType.EXCLUDE
        )
        self.assertTrue(excluded_item.is_carry_along)

        # 원본을 미리 삭제 -> source_daily_plan_item이 SET_NULL로 None이 됨
        self.item_2_1.delete()

        with self.assertRaises(RecoveryPlanStaleError):
            apply_recovery_plan(core_focus)

        # 적용이 거부됐으므로 상태 변경도 없어야 한다.
        core_focus.refresh_from_db()
        self.assertEqual(core_focus.status, RecoveryPlanStatus.PENDING)

    def test_cross_exam_competition_respects_importance_and_depth(self):
        """
        서로 다른 과목이 같은 시험일 + 제한된 용량(d1)을 두고 경쟁할 때,
        순서 보존 로직이 있어도 기존 스케줄러의 중요도/깊이 우선순위
        (high/core가 low/optional보다 먼저)가 유지돼야 한다.
        """
        shared_exam_date = self.today + timedelta(days=20)
        self.exam.exam_date = shared_exam_date
        self.exam.save(update_fields=["exam_date"])
        self.other_exam.exam_date = shared_exam_date
        self.other_exam.save(update_fields=["exam_date"])

        from planner.services.time_estimator import estimate_task_minutes
        _min, per_task_minutes = estimate_task_minutes(
            self.task_1_2.task_type, self.task_1_2.difficulty, self.exam.speed_factor
        )

        # 자료구조를 1-2와 정확히 같은 소요시간으로 맞춰서, 경쟁 결과가
        # 순수하게 중요도/깊이 차이로만 결정되게 한다.
        self.task_ds_1.importance = "low"
        self.task_ds_1.depth = "optional"
        self.task_ds_1.task_type = self.task_1_2.task_type
        self.task_ds_1.difficulty = self.task_1_2.difficulty
        self.task_ds_1.estimated_min_minutes = self.task_1_2.estimated_min_minutes
        self.task_ds_1.estimated_max_minutes = self.task_1_2.estimated_max_minutes
        self.task_ds_1.save(update_fields=[
            "importance", "depth", "task_type", "difficulty",
            "estimated_min_minutes", "estimated_max_minutes",
        ])

        ds_source_plan = DailyPlan.objects.create(
            exam_period=self.exam_period, date=self.today - timedelta(days=2),
            available_minutes=60, planned_minutes=60,
        )
        item_ds_failed = DailyPlanItem.objects.create(
            daily_plan=ds_source_plan, study_task=self.task_ds_1,
            planned_minutes=60, order=1,
        )
        record_progress(daily_plan_item=item_ds_failed, status="not_done", actual_minutes=0)
        ds_source_plan.finalized_at = django_timezone.now()
        ds_source_plan.save(update_fields=["finalized_at"])

        d1 = self.today + timedelta(days=1)

        # d1만 "딱 한 작업만 들어갈 용량"으로 좁게 만든다. setUp()에서
        # 이미 만들어둔 today+2~today+14(각 60분)는 그대로 둬서, d1을
        # 제외한 나머지 작업들은 전부 넉넉하게 배치될 수 있게 한다
        # (그래야 "d1 자리를 누가 먼저 차지하는가"만 순수하게 검증됨).
        AvailableTime.objects.update_or_create(
            exam_period=self.exam_period, date=d1,
            defaults={'available_minutes': per_task_minutes},
        )

        result = generate_recovery_options(self.source_plan, [self.item_1_2, item_ds_failed])
        maintain_volume = result['maintain_volume']
        self.assertIsNotNone(maintain_volume, result.get('maintain_volume_failure_reason'))

        item_1_2_recovery = maintain_volume.items.get(study_task=self.task_1_2)
        item_ds_recovery = maintain_volume.items.get(study_task=self.task_ds_1)

        # high/core인 1-2가 d1(딱 하나만 들어가는 자리)을 차지해야 하고,
        # low/optional인 자료구조는 그 뒤 날짜로 밀려야 한다.
        self.assertEqual(item_1_2_recovery.changed_date, d1)
        self.assertGreater(item_ds_recovery.changed_date, d1)

class RecoveryOccupiedCapacityDoubleCountTests(TestCase):
    """
    #181: 복구안 생성 시, 재배치 대상 작업 자신의 원래 자리가 여전히
    "점유 중"으로 계산되어 실제로는 배치 가능한 상황이 가용시간 부족으로
    잘못 실패하지 않는지 검증한다.
    """

    def setUp(self):
        from django.contrib.auth import get_user_model
        from exams.models import Exam, ExamPeriod, StudyTask

        User = get_user_model()
        self.user = User.objects.create_user(
            username="occupied_capacity_tester",
            email="occupied_capacity@example.com",
            password="pass1234",
        )
        self.client.login(username="occupied_capacity@example.com", password="pass1234")

        self.today = django_timezone.localdate()
        self.exam_period = ExamPeriod.objects.create(
            user=self.user,
            title="점유 중복계산 테스트",
            start_date=self.today - timedelta(days=3),
            end_date=self.today + timedelta(days=10),
            status="active",
        )
        self.exam = Exam.objects.create(
            exam_period=self.exam_period,
            subject_name="테스트 과목",
            exam_date=self.today + timedelta(days=10),
            speed_factor=1.0,
        )
        self.task_a = StudyTask.objects.create(
            exam=self.exam, title="A 작업", importance="medium", depth="basic",
            task_type="concept", difficulty="normal", order=1,
            estimated_min_minutes=60, estimated_max_minutes=60, is_confirmed=True,
        )

        # A 작업이 이미 배치돼 있던 계획 (오늘보다 이틀 전, 못함 처리 후 마감)
        self.source_plan = DailyPlan.objects.create(
            exam_period=self.exam_period, date=self.today - timedelta(days=2),
            available_minutes=60, planned_minutes=60,
        )
        self.item_a = DailyPlanItem.objects.create(
            daily_plan=self.source_plan, study_task=self.task_a,
            planned_minutes=60, order=1,
        )
        record_progress(daily_plan_item=self.item_a, status="not_done", actual_minutes=0)
        self.source_plan.finalized_at = django_timezone.now()
        self.source_plan.save(update_fields=["finalized_at"])

        # 미래 가용시간을 딱 A 작업(60분)이 들어갈 만큼만 준다.
        # 이중계산 버그가 있었다면 여기서 "부족"으로 잘못 실패했을 것.
        self.future_date = self.today + timedelta(days=1)
        AvailableTime.objects.create(
            exam_period=self.exam_period, date=self.future_date, available_minutes=60,
        )

    def test_maintain_volume_succeeds_with_exactly_needed_capacity(self):
        """
        딱 필요한 만큼(60분)의 가용시간만 있어도 분량유지형이 성공해야 한다.
        (원래 자리의 60분이 여전히 점유 중으로 이중계산되면, 실제로는
        충분한 이 상황도 부족으로 잘못 실패한다.)
        """
        result = generate_recovery_options(self.source_plan, [self.item_a])

        self.assertIsNotNone(
            result['maintain_volume'], result['maintain_volume_failure_reason']
        )
        item = result['maintain_volume'].items.get(study_task=self.task_a)
        self.assertEqual(item.changed_date, self.future_date)

        
class FeasibilityDeadlineAwareTests(TestCase):
    """
    #167: 전체 실현가능성 판정이 시험 당일 배제/작업 비분할을
    반영하는지 검증한다.
    """

    def setUp(self):
        from django.contrib.auth import get_user_model
        from exams.models import Exam, ExamPeriod, StudyTask

        User = get_user_model()
        self.user = User.objects.create_user(
            username="feasibility_tester",
            email="feasibility_tester@example.com",
            password="pass1234",
        )
        self.client.login(username="feasibility_tester@example.com", password="pass1234")

        self.today = django_timezone.localdate()
        self.exam_period = ExamPeriod.objects.create(
            user=self.user,
            title="전체 판정 정확도 테스트용 시험기간",
            start_date=self.today,
            end_date=self.today + timedelta(days=10),
            status="active",
        )

    def _feasibility_context(self):
        response = self.client.get(
            reverse('planner:feasibility', kwargs={'period_id': self.exam_period.id})
        )
        return response.context

    def test_exam_day_availability_excluded_from_overall_judgement(self):
        """
        시험 당일 가용시간은 그 과목 작업에 못 쓰이므로, 단순 합계로는
        '가능'인데 실제로는 불가능한 케이스를 잡아야 한다.

        시나리오: 시험이 내일(day+1)이고, 작업은 60분 필요한데
        오늘(day+0) 가용시간은 0분, 시험 당일(day+1) 가용시간만 60분.
        합계로는 60분 있어 보이지만, 시험 당일엔 그 작업을 못 배치하므로
        실제로는 불가능해야 한다.
        """
        from exams.models import Exam, StudyTask
        exam = Exam.objects.create(
            exam_period=self.exam_period, subject_name="테스트 과목",
            exam_date=self.today + timedelta(days=1),
            speed_factor=1.0,
        )
        StudyTask.objects.create(
            exam=exam, title="작업", importance="high", depth="core",
            task_type="concept", difficulty="normal", order=1,
            estimated_min_minutes=60, estimated_max_minutes=60, is_confirmed=True,
        )
        AvailableTime.objects.create(
            exam_period=self.exam_period, date=self.today, available_minutes=0,
        )
        AvailableTime.objects.create(
            exam_period=self.exam_period, date=self.today + timedelta(days=1),
            available_minutes=60,
        )

        context = self._feasibility_context()

        self.assertEqual(context['result']['status'], IMPOSSIBLE)
        self.assertFalse(context['can_generate'])

    def test_can_generate_false_when_fragmentation_prevents_allocation(self):
        """
        누적 판정은 통과해도(합계로는 충분해도), 작업이 쪼개지지 않아서
        실제로는 못 들어가는 경우 can_generate가 False여야 한다.

        시나리오: 60분짜리 작업 하나, 가용시간은 30분+30분으로 나뉜
        두 날짜뿐. 합계는 60분으로 충분해 보이지만 작업을 쪼갤 수 없어
        어느 한쪽에도 못 들어간다.
        """
        from exams.models import Exam, StudyTask
        exam = Exam.objects.create(
            exam_period=self.exam_period, subject_name="테스트 과목",
            exam_date=self.today + timedelta(days=10),
            speed_factor=1.0,
        )
        StudyTask.objects.create(
            exam=exam, title="60분 작업", importance="high", depth="core",
            task_type="concept", difficulty="normal", order=1,
            estimated_min_minutes=60, estimated_max_minutes=60, is_confirmed=True,
        )
        AvailableTime.objects.create(
            exam_period=self.exam_period, date=self.today, available_minutes=30,
        )
        AvailableTime.objects.create(
            exam_period=self.exam_period, date=self.today + timedelta(days=1),
            available_minutes=30,
        )

        context = self._feasibility_context()

        # 누적/합계 판정 자체는 통과할 수 있지만(60=60),
        # dry-run에서 파편화로 실패하므로 can_generate는 False여야 한다.
        self.assertFalse(context['can_generate'])

    def test_can_generate_true_for_normal_feasible_case(self):
        """
        정상적으로 배치 가능한 케이스에서는 can_generate가 True여야
        한다 (dry-run 추가로 인한 오탐 방지 확인용 회귀).
        """
        from exams.models import Exam, StudyTask
        exam = Exam.objects.create(
            exam_period=self.exam_period, subject_name="테스트 과목",
            exam_date=self.today + timedelta(days=10),
            speed_factor=1.0,
        )
        StudyTask.objects.create(
            exam=exam, title="작업", importance="high", depth="core",
            task_type="concept", difficulty="normal", order=1,
            estimated_min_minutes=30, estimated_max_minutes=30, is_confirmed=True,
        )
        AvailableTime.objects.create(
            exam_period=self.exam_period, date=self.today, available_minutes=60,
        )

        context = self._feasibility_context()

        self.assertEqual(context['result']['status'], POSSIBLE)
        self.assertTrue(context['can_generate'])

    def test_cumulative_min_max_tracked_separately_not_max_subtraction(self):
        """
        이전 consumed 방식의 회귀 반례: 이전 과목의 max만큼 무조건 소비했다고 가정하고
        빼는 방식(consumed -= max)은 min/max 폭이 좁은 뒤 과목을 실제보다
        비관적으로 판정한다.

        과목 A: min20/max100, A 시험일 이전 가용 50
        과목 B: min80/max80, B 시험일 이전 누적 가용 120

        올바른 누적 판정:
          A 시점: 누적 min20/max100, 가용50 -> 위험
          B 시점: 누적 min100/max180, 가용120 -> 위험
          전체 = 위험

        (consumed -= max 방식이었다면 B 시점 가용이 120-100=20으로 계산돼
        min80 미만이라 '불가능'으로 잘못 나왔을 것.)
        """
        from exams.models import Exam, StudyTask

        exam_a = Exam.objects.create(
            exam_period=self.exam_period, subject_name="A과목",
            exam_date=self.today + timedelta(days=3),
            speed_factor=1.0,
        )
        exam_b = Exam.objects.create(
            exam_period=self.exam_period, subject_name="B과목",
            exam_date=self.today + timedelta(days=6),
            speed_factor=1.0,
        )
        StudyTask.objects.create(
            exam=exam_a, title="A 작업", importance="high", depth="core",
            task_type="concept", difficulty="normal", order=1,
            estimated_min_minutes=20, estimated_max_minutes=100, is_confirmed=True,
        )
        StudyTask.objects.create(
            exam=exam_b, title="B 작업", importance="high", depth="core",
            task_type="concept", difficulty="normal", order=1,
            estimated_min_minutes=80, estimated_max_minutes=80, is_confirmed=True,
        )
        # A 시험일(day+3) 이전 가용시간 합 = 50
        AvailableTime.objects.create(
            exam_period=self.exam_period, date=self.today, available_minutes=50,
        )
        # B 시험일(day+6) 이전 누적 가용시간 = 50 + 70 = 120
        AvailableTime.objects.create(
            exam_period=self.exam_period, date=self.today + timedelta(days=4),
            available_minutes=70,
        )

        context = self._feasibility_context()

        self.assertEqual(context['result']['status'], RISKY)

    def test_subject_card_status_matches_cumulative_judgement(self):
        """
        _build_subject_results()(과목별 카드)도 전체 판정과 동일한
        누적 min/max 추적 방식을 써야 한다. 같은 반례로 검증:

        과목 A: min20/max100, A 시험일 이전 가용 50
        과목 B: min80/max80, B 시험일 이전 누적 가용 120

        예전(consumed -= max) 방식이면 B과목 카드가 '불가능'으로 잘못
        뜬다. 올바른 누적 판정이면 B과목도 '위험'이어야 한다.
        """
        from exams.models import Exam, StudyTask

        exam_a = Exam.objects.create(
            exam_period=self.exam_period, subject_name="A과목",
            exam_date=self.today + timedelta(days=3),
            speed_factor=1.0,
        )
        exam_b = Exam.objects.create(
            exam_period=self.exam_period, subject_name="B과목",
            exam_date=self.today + timedelta(days=6),
            speed_factor=1.0,
        )
        StudyTask.objects.create(
            exam=exam_a, title="A 작업", importance="high", depth="core",
            task_type="concept", difficulty="normal", order=1,
            estimated_min_minutes=20, estimated_max_minutes=100, is_confirmed=True,
        )
        StudyTask.objects.create(
            exam=exam_b, title="B 작업", importance="high", depth="core",
            task_type="concept", difficulty="normal", order=1,
            estimated_min_minutes=80, estimated_max_minutes=80, is_confirmed=True,
        )
        AvailableTime.objects.create(
            exam_period=self.exam_period, date=self.today, available_minutes=50,
        )
        AvailableTime.objects.create(
            exam_period=self.exam_period, date=self.today + timedelta(days=4),
            available_minutes=70,
        )

        context = self._feasibility_context()
        subject_results = {s['subject_name']: s for s in context['subject_results']}

        self.assertEqual(subject_results['A과목']['status'], RISKY)
        self.assertEqual(subject_results['B과목']['status'], RISKY)

class DashboardTodayLazyCheckTests(TestCase):
    """
    #173: dashboard()/today()도 period_list 등을 거치지 않고 바로 방문해도
    만료된 ACTIVE 시험기간을 COMPLETED로 자동 전환해야 한다 (화면 방문
    순서에 따라 표시 상태가 달라지는 불일치 방지).
    """

    def setUp(self):
        from django.contrib.auth import get_user_model
        from exams.models import ExamPeriod

        User = get_user_model()
        self.user = User.objects.create_user(
            username="lazy_check_tester",
            email="lazy_check_tester@example.com",
            password="pass1234",
        )
        self.client.login(username="lazy_check_tester@example.com", password="pass1234")

        self.today = django_timezone.localdate()
        self.expired_period = ExamPeriod.objects.create(
            user=self.user,
            title="만료된 시험기간",
            start_date=self.today - timedelta(days=10),
            end_date=self.today - timedelta(days=1),
            status="active",
        )

    def test_dashboard_visit_completes_expired_period(self):
        self.expired_period.refresh_from_db()
        self.assertEqual(self.expired_period.status, "active")

        self.client.get(reverse('planner:dashboard'))

        self.expired_period.refresh_from_db()
        self.assertEqual(self.expired_period.status, "completed")

    def test_today_visit_completes_expired_period(self):
        self.expired_period.refresh_from_db()
        self.assertEqual(self.expired_period.status, "active")

        self.client.get(reverse('planner:today'))

        self.expired_period.refresh_from_db()
        self.assertEqual(self.expired_period.status, "completed")

    def test_dashboard_does_not_touch_non_expired_period(self):
        """만료 안 된 ACTIVE 시험기간은 dashboard 방문으로 안 건드려야 한다."""
        self.expired_period.end_date = self.today + timedelta(days=5)
        self.expired_period.save(update_fields=["end_date"])

        self.client.get(reverse('planner:dashboard'))

        self.expired_period.refresh_from_db()
        self.assertEqual(self.expired_period.status, "active")