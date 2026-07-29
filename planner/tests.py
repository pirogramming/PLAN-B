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

from datetime import date

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