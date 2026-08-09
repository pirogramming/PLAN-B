"""
BE3 담당 - 캘린더 화면(planner:calendar)에 필요한 데이터를 계산한다.

이 파일이 하는 일:
- 요청받은 (year, month)의 달력 격자(6주 x 7일 = 42칸)를 만들고, 각 칸에
  그날의 포화도(계획시간/가능시간), 시험일 여부, 작업 미리보기(최대 3개)를 채운다.
- 날짜 클릭 시 프론트가 서버를 다시 안 부르고 그대로 뿌릴 수 있도록, 한 달치
  전체 작업 목록(day_details)도 같이 계산해서 내려준다.

월 이동은 페이지 전체 새로고침(<a href>)으로 처리하기로 확정했으므로, 이 파일은
"한 달치를 한 번에 계산"하는 것만 책임진다 (AJAX/JSON API 아님).

성능: 이 달에 걸친 AvailableTime/DailyPlan/DailyPlanItem을 각각 딱 한 번씩만
조회해서 파이썬에서 날짜별로 묶는다 (날짜 42칸마다 따로 쿼리하는 N+1 방지).
"""
from __future__ import annotations

import calendar as calendar_module
import datetime
from collections import defaultdict

from django.utils import timezone

from exams.models import AvailableTime, Exam
from planner.models import DailyPlan, DailyPlanItem

# 달력 미리보기 칸에 보여줄 작업 개수 (그 이상은 "+N개"로 뭉침)
PREVIEW_TASK_LIMIT = 3


def build_calendar_context(exam_period, year: int, month: int) -> dict:  # exam_period may be None
    """
    캘린더 화면에 필요한 컨텍스트(주간 격자 + 날짜별 상세)를 계산한다.

    Returns:
        {
            "weeks": [[cell, cell, ...] x 7, ...] x 6,   # 6주 x 7일 = 42칸
            "day_details": {"2026-10-14": {...}, ...},   # 작업이 있는 날짜만 포함
        }

    cell 하나의 구조:
        {
            "date": date,
            "in_period": bool,        # 이 시험기간(start_date~end_date) 안인지
            "is_today": bool,
            "is_exam_day": bool,
            "exam_subject": str | None,
            "available_minutes": int,
            "planned_minutes": int,
            "load_percent": int,      # 계획시간/가능시간*100 (포화도, 진행률 아님)
            "is_over": bool,          # load_percent > 100
            "tasks": [{"title": str, "subject_name": str, "shade_index": int}, ...],
            "more_count": int,        # tasks에 안 들어간 나머지 개수
        }
    """
    weeks_dates = _month_grid_dates(year, month)
    all_dates = [d for week in weeks_dates for d in week]
    min_date, max_date = min(all_dates), max(all_dates)

    # exam_period가 없으면(아직 시험기간을 등록하지 않은 사용자) 격자만 비어있는
    # 상태로 내려준다 - 아래 조회들은 전부 건너뛴다.
    if exam_period is None:
        exams = []
        available_by_date = {}
        daily_plans_by_date = {}
        items_by_date: dict[datetime.date, list] = defaultdict(list)
    else:
        exams = list(
            Exam.objects.filter(exam_period=exam_period).order_by("exam_date")
        )
        available_by_date = {
            at.date: at.available_minutes
            for at in AvailableTime.objects.filter(
                exam_period=exam_period, date__range=(min_date, max_date),
            )
        }
        daily_plans_by_date = {
            dp.date: dp
            for dp in DailyPlan.objects.filter(
                exam_period=exam_period, date__range=(min_date, max_date),
            )
        }

        items_by_date = defaultdict(list)
        items = (
            DailyPlanItem.objects
            .filter(daily_plan__in=daily_plans_by_date.values())
            .select_related("daily_plan", "study_task__exam", "progress_log")
            .order_by("order")  # 배치 순서 그대로 미리보기에 반영
        )
        for item in items:
            items_by_date[item.daily_plan.date].append(item)

    # 과목별 shade_index: 시험일 빠른 순으로 0, 1, 2...
    shade_index_by_subject = {
        exam.subject_name: i for i, exam in enumerate(exams)
    }
    exam_by_date = {exam.exam_date: exam.subject_name for exam in exams}

    today = timezone.localdate()

    weeks = []
    day_details = {}

    for week_dates in weeks_dates:
        week_cells = []
        for d in week_dates:
            day_items = items_by_date.get(d, [])
            daily_plan = daily_plans_by_date.get(d)

            # DailyPlan이 있으면 그 스냅샷 값을 그대로 쓰고(생성 시점 기준으로
            # 이미 확정된 값이라 신뢰할 수 있음), 아직 계획이 없는 날짜는
            # AvailableTime만으로 available_minutes를 보여준다.
            if daily_plan is not None:
                available_minutes = daily_plan.available_minutes
                planned_minutes = daily_plan.planned_minutes
            else:
                available_minutes = available_by_date.get(d, 0)
                planned_minutes = 0

            load_percent = (
                round(planned_minutes / available_minutes * 100)
                if available_minutes else 0
            )

            preview_tasks = [
                {
                    "title": item.study_task.title,
                    "subject_name": item.study_task.exam.subject_name,
                    "shade_index": shade_index_by_subject.get(
                        item.study_task.exam.subject_name, 0
                    ),
                }
                for item in day_items[:PREVIEW_TASK_LIMIT]
            ]

            in_period = (
                exam_period is not None
                and exam_period.start_date <= d <= exam_period.end_date
            )

            week_cells.append({
                "date": d,
                "in_period": in_period,
                "is_today": d == today,
                "is_exam_day": d in exam_by_date,
                "exam_subject": exam_by_date.get(d),
                "available_minutes": available_minutes,
                "planned_minutes": planned_minutes,
                "load_percent": load_percent,
                "is_over": load_percent > 100,
                "tasks": preview_tasks,
                "more_count": max(0, len(day_items) - PREVIEW_TASK_LIMIT),
            })

            if day_items:
                day_details[d.isoformat()] = {
                    "available_minutes": available_minutes,
                    "planned_minutes": planned_minutes,
                    "tasks": [_task_row_fields(item) for item in day_items],
                }

        weeks.append(week_cells)

    return {"weeks": weeks, "day_details": day_details}


def _month_grid_dates(year: int, month: int) -> list[list[datetime.date]]:
    """
    (year, month)를 6주 x 7일 = 42칸으로 고정된 격자로 반환한다 (일요일 시작).
    Calendar.monthdatescalendar()는 달에 따라 4~6주를 반환하므로, 6주 미만이면
    다음 달 날짜로 패딩해서 항상 6주로 맞춘다 (프론트가 매번 다른 행 수를
    처리할 필요 없게).
    """
    cal = calendar_module.Calendar(firstweekday=6)  # 6 = 일요일 시작
    weeks = cal.monthdatescalendar(year, month)

    while len(weeks) < 6:
        next_start = weeks[-1][-1] + datetime.timedelta(days=1)
        weeks.append([next_start + datetime.timedelta(days=i) for i in range(7)])

    return weeks


def _task_row_fields(item: DailyPlanItem) -> dict:
    """
    day_details의 작업 하나를 today.html의 task_row 컴포넌트가 쓰는 필드
    그대로 맞춰서 반환한다 (FE2 요청 - 컴포넌트 재사용).
    """
    log = getattr(item, "progress_log", None)
    return {
        "title": item.study_task.title,
        "subject_name": item.study_task.exam.subject_name,
        "depth": item.study_task.depth,
        "planned_minutes": item.planned_minutes,
        "status": item.status,
        "actual_minutes": log.actual_minutes if log else None,
        "completion_percent": log.completion_percent if log else None,
    }