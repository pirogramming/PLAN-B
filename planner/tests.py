from django.test import TestCase
from datetime import timedelta
from django.utils import timezone as django_timezone

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