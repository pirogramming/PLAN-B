from django.test import TestCase

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