import json
from django.http import JsonResponse
from planner.models import DailyPlan, DailyPlanItem, RecoveryPlan, ProgressLog
from planner.services.progress_recorder import record_progress, FinalizedDailyPlanEditError
from django.contrib.auth.decorators import login_required
from datetime import timedelta
from collections import defaultdict
from django.db.models import Sum
from django.contrib import messages
from django.http import JsonResponse
from django.http import Http404
from core.choices import RecoveryActionType, RecoveryType, TaskDepth
from planner.services.recovery import get_future_available_capacity
from planner.services.time_estimator import estimate_task_minutes, round_up_to_five
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods
from core.choices import ExamPeriodStatus, RecoveryPlanStatus, ProgressStatus
from django.utils import timezone
from django.urls import reverse
from exams.models import ExamPeriod, StudyTask, AvailableTime
from planner.services.feasibility_checker import calculate_feasibility, POSSIBLE, RISKY, IMPOSSIBLE
from planner.services.schedule_generator import (
    generate_schedule,
    ScheduleAlreadyExistsError,
    UnallocatedTasksError,
    MismatchedExamPeriodError,
    DuplicateTaskAllocationError,
)
from planner.services.calendar import build_calendar_context
from planner.services.progress_recorder import (
    finalize_daily_plan,
    DailyPlanAlreadyFinalizedError,
)
from planner.services.recovery import (
    apply_recovery_plan,
    RecoveryPlanAlreadyProcessedError,
    RecoveryPlanStaleError,
    RecoveryPlanInvalidDataError,
)
import logging

logger = logging.getLogger(__name__)


def _get_owned_exam_period(user, period_id):
    return get_object_or_404(ExamPeriod, id=period_id, user=user)


def _confirmed_tasks(exam_period):
    return StudyTask.objects.filter(
        exam__exam_period=exam_period, is_confirmed=True
    ).select_related('exam')

def _get_pending_recovery(exam_period):
    """
    exam_period 전체 범위에서 아직 선택 안 된 복구안을 조회한다.
    (dashboard, today 양쪽에서 공유 — 한쪽만 고치는 사고 방지)
    """
    pending_recovery_qs = RecoveryPlan.objects.filter(
        source_daily_plan__exam_period=exam_period, status=RecoveryPlanStatus.PENDING
    )
    pending_recovery_item = pending_recovery_qs.order_by('-created_at').first()

    if pending_recovery_item is None:
        return None

    return {
        'recovery_group_id': pending_recovery_item.recovery_group_id,
        'created_at': pending_recovery_item.created_at,
        'count': pending_recovery_qs.values('recovery_group_id').distinct().count(),
    }

def _remaining_task_minutes(task):
    """
    아직 안 끝난 작업의 남은 필요시간(min/max).
    recovery.py의 _remaining_minutes()와 같은 방식(최신 speed_factor로 재추정 +
    완료율 반영)이지만, "오늘 계획" 범위가 아니라 이 작업의 가장 최근 ProgressLog
    전체를 본다 (대시보드는 시험기간 전체 기준이라 특정 날짜에 묶이지 않음).
    """
    latest_log = (
        ProgressLog.objects
        .filter(daily_plan_item__study_task=task)
        .order_by('-recorded_at')
        .first()
    )
    est_min, est_max = estimate_task_minutes(
        task.task_type, task.difficulty, task.exam.speed_factor
    )
    if latest_log is None or latest_log.progress_status == ProgressStatus.NOT_DONE:
        return est_min, est_max
    if latest_log.progress_status == ProgressStatus.DONE:
        return 0, 0
    # PARTIAL
    remaining_ratio = max(100 - (latest_log.completion_percent or 0), 0) / 100
    return (
        round_up_to_five(est_min * remaining_ratio),
        round_up_to_five(est_max * remaining_ratio),
    )


def _build_overall(exam_period, remaining_days):
    """
    시험기간 전체 기준 Fit Bar. recovery_compare의 _fit_bar_context()와
    같은 공식(A~B 구간 + axis_max = max(가용,필요) * 1.15)을 재사용한다.
    """
    tasks = list(_confirmed_tasks(exam_period))
    required_min = required_max = 0
    for task in tasks:
        mn, mx = _remaining_task_minutes(task)
        required_min += mn
        required_max += mx

    # 주의: get_future_available_capacity()는 이미 배치된 DailyPlanItem만큼
    # 뺀 "순수 잔여" 값이라 여기서 쓰면 안 된다. required_min/max가 이미
    # 배치된 미완료 작업의 남은 시간을 포함하고 있어서, 그걸 또 available에서
    # 빼면 같은 시간이 이중으로 깎인다 (recovery.py는 "새 작업을 끼워넣을
    # 자리"를 찾는 거라 순수 잔여가 맞지만, 여기는 "필요 vs 원래 가진 시간"
    # 비교라 원본 AvailableTime 총량을 써야 한다).
    tomorrow = timezone.localdate() + timedelta(days=1)
    available_minutes = AvailableTime.objects.filter(
        exam_period=exam_period, date__gte=tomorrow
    ).aggregate(total=Sum('available_minutes'))['total'] or 0

    result = calculate_feasibility(required_min, required_max, available_minutes)
    status_label_map = {POSSIBLE: "가능", RISKY: "위험", IMPOSSIBLE: "불가능"}

    axis_max = max(available_minutes, required_max, 1) * 1.15
    min_pct = round(required_min / axis_max * 100, 1)
    max_pct = round(required_max / axis_max * 100, 1)

    daily_extra_minutes = (
        round(result["shortage_minutes"] / remaining_days)
        if remaining_days and result["shortage_minutes"] else 0
    )

    return {
        "status": result["status"],
        "status_label": status_label_map[result["status"]],
        "viewed_at": timezone.now(),
        "min_minutes": required_min,
        "max_minutes": required_max,
        "available_minutes": available_minutes,
        "max_shortage_minutes": result["shortage_minutes"],
        "daily_extra_minutes": daily_extra_minutes,
        "min_pct": min_pct,
        "band_pct": round(max_pct - min_pct, 1),
        "mark_pct": round(min(available_minutes / axis_max * 100, 100), 1),
        "need_label_pct": round((min_pct + max_pct) / 2, 1),
        "axis_max": round(axis_max),
    }


def _build_subject_summary(exam_period):
    today = timezone.localdate()
    summary = []
    for exam in exam_period.exams.all().order_by('exam_date'):
        subject_tasks = _confirmed_tasks(exam_period).filter(exam=exam)
        remaining_minutes = 0
        remaining_task_count = 0
        for task in subject_tasks:
            _mn, mx = _remaining_task_minutes(task)
            if mx > 0:
                remaining_minutes += mx
                remaining_task_count += 1

        d_day = (exam.exam_date - today).days
        summary.append({
            "subject_name": exam.subject_name,
            "exam_date": exam.exam_date,
            "d_day": d_day,
            "is_near": d_day <= 3,
            "remaining_minutes": remaining_minutes,
            "remaining_task_count": remaining_task_count,
        })
    return summary


def _build_progress(exam_period, today_plan, today_count, today_minutes):
    """
    완료 크레딧 방식은 today()와 동일: DONE은 planned_minutes 전액,
    PARTIAL은 completion_percent 비율만큼만 인정.
    today_*는 오늘 하루, total_*는 이 시험기간에 지금까지 생성된 모든
    DailyPlanItem 누적 기준이다.
    """
    def _credit(items):
        total = 0
        done = 0
        for item in items:
            planned = item.planned_minutes
            total += planned
            log = getattr(item, 'progress_log', None)
            if log is None:
                continue
            if log.progress_status == ProgressStatus.DONE:
                done += planned
            elif log.progress_status == ProgressStatus.PARTIAL:
                done += planned * (log.completion_percent or 0) // 100
        return total, done

    today_items = list(
        today_plan.items.select_related('progress_log')
    ) if today_plan else []
    today_total, today_done = _credit(today_items)

    all_items = list(
        DailyPlanItem.objects
        .filter(daily_plan__exam_period=exam_period)
        .select_related('progress_log', 'study_task')
    )
    total_total, total_done = _credit(all_items)

    core_left = sum(
        1 for task in _confirmed_tasks(exam_period)
        if task.depth == TaskDepth.CORE and _remaining_task_minutes(task)[1] > 0
    )

    return {
        "today_percent": round(today_done / today_total * 100) if today_total else 0,
        "today_done_minutes": today_done,
        "today_total_minutes": today_total,
        "total_percent": round(total_done / total_total * 100) if total_total else 0,
        "total_done_minutes": total_done,
        "total_minutes": total_total,
        "core_left": core_left,
    }

def _available_times(exam_period):
    """
    계획 배치에 쓸 가용시간은 항상 '오늘 이후'만 넘긴다. 스케줄러는
    시험일 이전인지만 검사하고 오늘 이전인지는 검사하지 않으므로,
    여기서 걸러주지 않으면 과거 날짜에 작업이 배치될 수 있다.
    """
    planning_start = max(exam_period.start_date, timezone.localdate())
    return AvailableTime.objects.filter(
        exam_period=exam_period,
        date__gte=planning_start,
        date__lte=exam_period.end_date,
    ).order_by('date')


def _calculate_feasibility_for_period(exam_period):
    tasks = list(_confirmed_tasks(exam_period))
    required_min = sum(t.estimated_min_minutes for t in tasks)
    required_max = sum(t.estimated_max_minutes for t in tasks)
    available = sum(at.available_minutes for at in _available_times(exam_period))
    result = calculate_feasibility(required_min, required_max, available)
    return result, tasks

def _build_subject_results(exam_period, tasks):
    """
    과목별 카드 표시용 데이터 + 시험일 순서 누적 검증 기반 실현가능성 판정.

    실제 스케줄러가 "시험일 빠른 과목 우선 배치" 정책을 쓰는 것과 같은 원리로,
    각 과목 시험일 이전까지의 가용시간에서 앞선 과목들이 이미 쓴 만큼을 뺀
    나머지를 그 과목의 가용시간으로 보고 판정한다. 완벽한 정답(실제 빈 패킹
    시뮬레이션)은 아니지만, 합계만 보던 기존 방식보다 훨씬 정확하다.
    """
    status_label_map = {POSSIBLE: "가능", RISKY: "위험", IMPOSSIBLE: "불가능"}
    available_times = list(_available_times(exam_period))
    subject_results = []
    consumed = 0

    for exam in exam_period.exams.all().order_by('exam_date'):
        subject_tasks = [task for task in tasks if task.exam_id == exam.id]

        required_min = sum(task.estimated_min_minutes for task in subject_tasks)
        required_max = sum(task.estimated_max_minutes for task in subject_tasks)

        capacity_until_exam = sum(
            at.available_minutes for at in available_times
            if at.date < exam.exam_date
        )
        available_for_subject = max(capacity_until_exam - consumed, 0)

        feasibility_result = calculate_feasibility(
            required_min, required_max, available_for_subject
        )

        subject_results.append({
            'exam_id': exam.id,
            'subject_name': exam.subject_name,
            'exam_date': exam.exam_date,
            'task_count': len(subject_tasks),
            'required_min_minutes': required_min,
            'required_recommended_minutes': required_max,
            'status': feasibility_result['status'],
            'status_label': status_label_map[feasibility_result['status']],
        })

        consumed += required_max

    return subject_results

def _validate_task_readiness(exam_period):
    """
    가능성 판정·계획 생성 전에 데이터가 실제로 준비됐는지 확인한다.
    과목이 여러 개인데 일부만 확정된 상태로 조용히 계획이 생성되는 걸 막는다.
    """
    exams = exam_period.exams.all()

    if not exams.exists():
        return False, "등록된 과목이 없습니다."

    all_tasks = StudyTask.objects.filter(exam__exam_period=exam_period)

    if not all_tasks.exists():
        return False, "등록된 학습 작업이 없습니다."

    if all_tasks.filter(is_confirmed=False).exists():
        return False, "아직 확정되지 않은 학습 작업이 있습니다."

    if exams.exclude(study_tasks__is_confirmed=True).exists():
        return False, "학습 작업이 확정되지 않은 과목이 있습니다."

    if all_tasks.filter(estimated_max_minutes__lte=0).exists():
        return False, "예상시간이 계산되지 않은 학습 작업이 있습니다."

    return True, None


@login_required
@require_http_methods(["GET"])
def feasibility(request, period_id):
    exam_period = _get_owned_exam_period(request.user, period_id)

    is_ready, readiness_error = _validate_task_readiness(exam_period)
    result, tasks = _calculate_feasibility_for_period(exam_period)

    subject_results = _build_subject_results(exam_period, tasks)

    context = {
        'exam_period': exam_period,
        'result': result,
        'subject_results': subject_results,
        'total_min_minutes': result['required_min_minutes'],
        'total_max_minutes': result['required_recommended_minutes'],
        'total_available_minutes': result['available_minutes'],
        'task_count': len(tasks),
        'can_generate': is_ready and result['status'] == POSSIBLE,
        'readiness_error': readiness_error,
        'available_time_edit_url': reverse(
            'exams:available_time_update', kwargs={'period_id': exam_period.id}
        ),
    }
    return render(request, 'planner/feasibility.html', context)


@login_required
@require_http_methods(["POST"])
def plan_generate(request, period_id):
    exam_period = _get_owned_exam_period(request.user, period_id)

    is_ready, readiness_error = _validate_task_readiness(exam_period)
    if not is_ready:
        messages.error(request, readiness_error)
        return redirect('planner:feasibility', period_id=exam_period.id)

    result, tasks = _calculate_feasibility_for_period(exam_period)
    if result['status'] != POSSIBLE:
        messages.error(
            request,
            "현재 상태에서는 계획을 생성할 수 없습니다. 가능시간 또는 학습작업을 조정해주세요.",
        )
        return redirect('planner:feasibility', period_id=exam_period.id)

    available_times = list(_available_times(exam_period))

    try:
        generate_schedule(
            exam_period=exam_period,
            study_tasks=tasks,
            available_times=available_times,
        )
    except ScheduleAlreadyExistsError:
        messages.info(request, "이미 생성된 계획이 있습니다.")
        return redirect('planner:plan_complete', period_id=exam_period.id)
    except UnallocatedTasksError:
        messages.error(
            request,
            "전체 가능시간은 충분하지만 시험일 또는 날짜별 가능시간 제약으로 "
            "일부 작업을 배치하지 못했습니다. 날짜별 가능시간을 조정해주세요.",
        )
        return redirect('planner:feasibility', period_id=exam_period.id)
    except (MismatchedExamPeriodError, DuplicateTaskAllocationError):
        messages.error(request, "계획 생성 중 데이터 오류가 발생했습니다.")
        return redirect('planner:feasibility', period_id=exam_period.id)

    return redirect('planner:plan_complete', period_id=exam_period.id)


@login_required
@require_http_methods(["GET"])
def plan_complete(request, period_id):
    exam_period = _get_owned_exam_period(request.user, period_id)
    daily_plans = (
        DailyPlan.objects
        .filter(exam_period=exam_period)
        .order_by('date')
        .prefetch_related('items')
    )

    if not daily_plans.exists():
        messages.info(request, "아직 생성된 계획이 없습니다.")
        return redirect('planner:feasibility', period_id=exam_period.id)

    total_planned_minutes = sum(dp.planned_minutes for dp in daily_plans)

    context = {
        'exam_period': exam_period,
        'daily_plans': daily_plans,
        'daily_plan_count': daily_plans.count(),
        'total_planned_minutes': total_planned_minutes,
    }
    return render(request, 'planner/plan_complete.html', context)

@login_required
@require_http_methods(["GET"])
def dashboard(request):
    """
    시험기간/계획 존재 여부에 따라 온보딩 화면 또는 전체 대시보드를 보여준다.
    """
    exam_period = (
        ExamPeriod.objects
        .filter(user=request.user, status=ExamPeriodStatus.ACTIVE)
        .order_by('-created_at')
        .first()
    )

    if exam_period is None:
        return render(request, 'planner/dashboard.html', {'exam_period': None})

    has_plan = DailyPlan.objects.filter(exam_period=exam_period).exists()

    if not has_plan:
        is_ready, _ = _validate_task_readiness(exam_period)
        if is_ready:
            next_step_label, next_step_url = "계획 생성하기", reverse(
                'planner:feasibility', kwargs={'period_id': exam_period.id}
            )
        else:
            next_step_label, next_step_url = "학습 작업 확인하기", reverse(
                'exams:period_detail', kwargs={'period_id': exam_period.id}
            )

        context = {
            'exam_period': exam_period,
            'has_plan': False,
            'next_step_label': next_step_label,
            'next_step_url': next_step_url,
        }
        return render(request, 'planner/dashboard.html', context)

    today = timezone.localdate()
    today_plan = DailyPlan.objects.filter(exam_period=exam_period, date=today).first()
    today_count = today_plan.items.count() if today_plan else 0
    today_minutes = today_plan.planned_minutes if today_plan else 0

    remaining_days = len(
        {at.date for at in get_future_available_capacity(exam_period, today)}
    )

    context = {
        'exam_period': exam_period,
        'has_plan': True,
        'today': today,
        'today_count': today_count,
        'today_minutes': today_minutes,
        'pending_recovery': _get_pending_recovery(exam_period),
        'remaining_days': remaining_days,
        'overall': _build_overall(exam_period, remaining_days),
        'subject_summary': _build_subject_summary(exam_period),
        'progress': _build_progress(exam_period, today_plan, today_count, today_minutes),
    }
    return render(request, 'planner/dashboard.html', context)

@login_required
@require_http_methods(["GET"])
def today(request):
    today_date = timezone.localdate()

    exam_period = (
        ExamPeriod.objects
        .filter(user=request.user, status=ExamPeriodStatus.ACTIVE)
        .order_by('-created_at')
        .first()
    )

    context = {
        'today': today_date,
        'exam_period': exam_period,
        'today_count': 0,
        'tasks': [],
        'is_finalized': False,
        'pending_recovery': None,
        'calendar_url': reverse('planner:calendar'),
    }

    if exam_period is None:
        context.update({
            'empty_title': "등록된 시험기간이 없습니다",
            'empty_desc': "먼저 시험기간을 등록해주세요.",
        })
        return render(request, 'planner/today.html', context)

    pending_recovery = _get_pending_recovery(exam_period)
    if pending_recovery:
        context['pending_recovery'] = pending_recovery

    today_plan = DailyPlan.objects.filter(exam_period=exam_period, date=today_date).first()

    if today_plan is None:
        return render(request, 'planner/today.html', context)

    items = list(
        today_plan.items
        .select_related('study_task__exam', 'progress_log')
        .order_by('order', 'id')
    )

    tasks = []
    total_minutes = 0
    done_progress_minutes = 0
    partial_progress_minutes = 0
    done_actual_minutes = 0
    partial_actual_minutes = 0
    done_count = 0
    partial_count = 0
    not_done_count = 0
    pending_count = 0

    for item in items:
        planned_minutes = item.planned_minutes
        total_minutes += planned_minutes

        log = getattr(item, 'progress_log', None)
        status = log.progress_status if log else None

        if status == ProgressStatus.DONE:
            done_count += 1
            done_progress_minutes += planned_minutes
            done_actual_minutes += log.actual_minutes or 0
        elif status == ProgressStatus.PARTIAL:
            partial_count += 1
            partial_actual_minutes += log.actual_minutes or 0
            partial_progress_minutes += planned_minutes * (log.completion_percent or 0) // 100
        elif status == ProgressStatus.NOT_DONE:
            not_done_count += 1
        else:
            pending_count += 1

        tasks.append({
            'id': item.id,
            'status': status,
            'subject_name': item.study_task.exam.subject_name,
            'depth': item.study_task.depth,
            'title': item.study_task.title,
            'completion_percent': log.completion_percent if log else None,
            'actual_minutes': log.actual_minutes if log else None,
            'planned_minutes': planned_minutes,
        })

    # 주의: recovery.py의 _remaining_minutes()와 "completion_percent로 남은 비율을
    # 계산한다"는 방식은 같지만, 기준값이 다르다.
    # - recovery.py: 복구 시점의 최신 speed_factor로 다시 계산한 estimated_max 사용
    #   (미래 재배치를 위한 보수적 재추정)
    # - 여기(today): 스케줄링 당시 저장된 planned_minutes 스냅샷 사용
    #   (오늘 계획 대비 진행률 표시 목적)
    remaining_minutes = max(total_minutes - done_progress_minutes - partial_progress_minutes, 0)
    done_percent = round(done_progress_minutes / total_minutes * 100) if total_minutes else 0
    partial_percent = round(partial_progress_minutes / total_minutes * 100) if total_minutes else 0

    context.update({
        'tasks': tasks,
        'today_count': len(tasks),
        'is_finalized': today_plan.finalized_at is not None,
        'summary': {
            'remaining_minutes': remaining_minutes,
            'done_minutes': done_progress_minutes,
            'partial_minutes': partial_progress_minutes,
            'total_minutes': total_minutes,
            'done_percent': done_percent,
            'partial_percent': partial_percent,
            'pending_count': pending_count,
        },
        'eod': {
            'done_count': done_count,
            'done_minutes': done_actual_minutes,
            'partial_count': partial_count,
            'partial_minutes': partial_actual_minutes,
            'not_done_count': not_done_count + pending_count,
            'not_done_minutes': 0,
            'pending_count': pending_count,
        },
    })
    return render(request, 'planner/today.html', context)

@login_required
@require_http_methods(["GET"])
def calendar(request):
    exam_period = (
        ExamPeriod.objects
        .filter(user=request.user, status=ExamPeriodStatus.ACTIVE)
        .order_by('-created_at')
        .first()
    )

    today_date = timezone.localdate()
    try:
        year = int(request.GET.get('year', today_date.year))
        month = int(request.GET.get('month', today_date.month))
    except (TypeError, ValueError):
        year, month = today_date.year, today_date.month

    # 리뷰 반영: year/month의 최종 검증·보정은 build_calendar_context()가 이미
    # 책임지고 있다 (1<=month<=12, MINYEAR<=year<=MAXYEAR 범위 체크뿐 아니라,
    # year=9999·month=12처럼 "형식은 유효하지만 6주 격자 패딩이 연도 경계를
    # 넘는" 경계 케이스까지). 그래서 여기서는 정수 변환 실패만 방어하고,
    # 나머지 검증은 그쪽에 맡긴다.
    #
    # 중요: build_calendar_context()가 내부에서 값을 보정해도 그 사실이 이
    # 함수의 지역변수 year/month에는 반영되지 않는다. prev/next 링크를 계산할
    # 때 이 지역변수를 그대로 쓰면(과거에 그랬던 것처럼), 화면 제목은
    # "2026년 8월"인데 "다음 달" 링크는 next_year=10000처럼 깨진 값을 가리키는
    # 불일치가 생긴다. 그래서 반환값에서 실제로 사용된 year/month를 다시
    # 받아와 그 값 기준으로 prev/next를 계산해야 한다.
    calendar_context = build_calendar_context(exam_period, year, month)
    year = calendar_context["year"]
    month = calendar_context["month"]

    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)

    context = {
        'exam_period': exam_period,
        'year': year,
        'month': month,
        'prev_year': prev_year,
        'prev_month': prev_month,
        'next_year': next_year,
        'next_month': next_month,
        **calendar_context,
    }
    return render(request, 'planner/calendar.html', context)

@login_required
@require_http_methods(["POST"])
def progress_record(request, item_id):
    item = (
        DailyPlanItem.objects
        .select_related("daily_plan", "study_task__exam")
        .filter(
            id=item_id,
            daily_plan__exam_period__user=request.user,
            daily_plan__exam_period__status=ExamPeriodStatus.ACTIVE,
            daily_plan__date=timezone.localdate(),
        )
        .first()
    )

    if item is None:
        return JsonResponse(
            {"message": "오늘 학습 작업을 찾을 수 없습니다."},
            status=404,
        )

    try:
        payload = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse(
            {"message": "올바른 JSON 요청이 아닙니다."},
            status=400,
        )

    if not isinstance(payload, dict):
        return JsonResponse(
            {"message": "요청 본문은 JSON 객체여야 합니다."},
            status=400,
        )

    status = payload.get("status")
    actual_minutes = payload.get("actual_minutes")
    completion_percent = payload.get("completion_percent")

    if status not in ProgressStatus.values:
        return JsonResponse(
            {"message": "올바르지 않은 학습 상태입니다."},
            status=400,
        )

    if status in (ProgressStatus.DONE, ProgressStatus.PARTIAL):
        if (
            isinstance(actual_minutes, bool)
            or not isinstance(actual_minutes, int)
            or not 1 <= actual_minutes <= 1439
        ):
            return JsonResponse(
                {"message": "실제 공부시간은 1~1439분 사이의 정수여야 합니다."},
                status=400,
            )
    else:
        actual_minutes = None

    if status == ProgressStatus.PARTIAL:
        if (
            isinstance(completion_percent, bool)
            or not isinstance(completion_percent, int)
            or not 1 <= completion_percent <= 99
        ):
            return JsonResponse(
                {"message": "일부완료 진행률은 1~99 사이의 정수여야 합니다."},
                status=400,
            )
    else:
        completion_percent = None

    try:
        result = record_progress(
            daily_plan_item=item,
            status=status,
            actual_minutes=actual_minutes,
            completion_percent=completion_percent,
        )
    except FinalizedDailyPlanEditError as exc:
        return JsonResponse({"message": str(exc)}, status=409)
    except (ValueError, TypeError) as exc:
        return JsonResponse({"message": str(exc)}, status=400)

    progress_log = result["progress_log"]

    return JsonResponse({
        "item_id": item.id,
        "status": progress_log.progress_status,
        "actual_minutes": progress_log.actual_minutes,
        "completion_percent": progress_log.completion_percent,
        "daily_plan_status": result["daily_plan_status"],
    })


def _get_today_daily_plan(user):
    return (
        DailyPlan.objects
        .filter(
            exam_period__user=user,
            exam_period__status=ExamPeriodStatus.ACTIVE,
            date=timezone.localdate(),
        )
        .order_by("-exam_period__created_at")
        .first()
    )


@login_required
@require_http_methods(["POST"])
def daily_plan_finalize(request):
    daily_plan = _get_today_daily_plan(request.user)

    if daily_plan is None:
        return JsonResponse(
            {"message": "오늘 마감할 계획이 없습니다."},
            status=404,
        )

    try:
        result = finalize_daily_plan(
            daily_plan,
            mark_unrecorded_as_not_done=True,
        )
    except DailyPlanAlreadyFinalizedError:
        return JsonResponse(
            {"message": "이미 마감된 계획입니다."},
            status=409,
        )

    recovery_plans = result["recovery_plans"] or {}
    recovery_plan = (
        recovery_plans.get("maintain_volume")
        or recovery_plans.get("core_focus")
    )

    recovery_group_id = (
        str(recovery_plan.recovery_group_id)
        if recovery_plan
        else None
    )

    return JsonResponse({
        "needs_recovery": result["needs_recovery"],
        "recovery_available": recovery_plan is not None,
        "recovery_group_id": recovery_group_id,
        "auto_marked_not_done_count": (
            result["auto_marked_not_done_count"]
        ),
    })

def _fit_bar_context(min_minutes, max_minutes, available_minutes, axis_max):
    min_pct = round(min_minutes / axis_max * 100, 1)
    max_pct = round(max_minutes / axis_max * 100, 1)
    return {
        "min_pct": min_pct,
        "band_pct": round(max_pct - min_pct, 1),
        "mark_pct": round(min(available_minutes / axis_max * 100, 100), 1),
        "need_label_pct": round((min_pct + max_pct) / 2, 1),
        "min_minutes": min_minutes,
        "max_minutes": max_minutes,
        "available_minutes": available_minutes,
        "axis_max": round(axis_max),
    }


def _item_min_max_minutes(item):
    task = item.study_task
    est_min, est_max = estimate_task_minutes(
        task.task_type, task.difficulty, task.exam.speed_factor
    )
    if est_max == 0:
        return 0, item.remaining_minutes
    ratio = est_min / est_max
    return round(item.remaining_minutes * ratio), item.remaining_minutes


def _plan_totals(recovery_plan):
    items = list(recovery_plan.items.all())
    reschedule_items = [i for i in items if i.action_type == RecoveryActionType.RESCHEDULE]
    excluded_items = [i for i in items if i.action_type == RecoveryActionType.EXCLUDE]

    min_total = max_total = 0
    for item in reschedule_items:
        mn, mx = _item_min_max_minutes(item)
        min_total += mn
        max_total += mx

    return {
        "reschedule_items": reschedule_items,
        "excluded_items": excluded_items,
        "min_total": min_total,
        "max_total": max_total,
    }


def _build_plan_context(recovery_plan, totals, available_minutes, axis_max):
    min_total, max_total = totals["min_total"], totals["max_total"]
    excluded_items = totals["excluded_items"]
    reschedule_items = totals["reschedule_items"]

    if available_minutes >= max_total:
        status, status_label, shortage = "ok", "지금 가능", 0
    elif available_minutes >= min_total:
        status, status_label, shortage = "warn", "추가 시간 필요", max_total - available_minutes
    else:
        status, status_label, shortage = "bad", "적용 불가", min_total - available_minutes

    distinct_days = {i.changed_date for i in reschedule_items if i.changed_date}
    daily_average_minutes = round(max_total / len(distinct_days)) if distinct_days else None

    type_label = "분량 유지형" if recovery_plan.recovery_type == RecoveryType.MAINTAIN_VOLUME else "핵심 집중형"
    desc = (
        "작업을 빼지 않고 남은 날짜에 다시 배치합니다."
        if recovery_plan.recovery_type == RecoveryType.MAINTAIN_VOLUME
        else "우선순위가 낮은 작업부터 제외하고 남은 날짜에 배치합니다."
    )

    return {
        "id": recovery_plan.id,
        "recovery_type": recovery_plan.recovery_type,
        "type_label": type_label,
        "preview_url": reverse(
            "planner:recovery_preview",
            kwargs={"plan_id": recovery_plan.id},
        ),
        "summary": desc,
        "feasibility_status": status,
        "feasibility_status_label": status_label,
        "excluded_count": len(excluded_items),
        "excluded_minutes": sum(i.remaining_minutes for i in excluded_items),
        "total_minutes": max_total,
        "daily_average_minutes": daily_average_minutes,
        "daily_diff": None,
        "extra_minutes_needed": shortage,
        "excluded_tasks": [
            {
                "subject_name": i.study_task.exam.subject_name,
                "title": i.study_task.title,
                "depth": i.study_task.depth,
                "importance": i.study_task.importance,
                "minutes": i.remaining_minutes,
            }
            for i in excluded_items
        ],
        **_fit_bar_context(min_total, max_total, available_minutes, axis_max),
    }

def _speed_added_minutes(items):
    added = 0
    for item in items:
        task = item.study_task
        _base_min, base_max = estimate_task_minutes(task.task_type, task.difficulty, 1.0)
        _real_min, real_max = estimate_task_minutes(task.task_type, task.difficulty, task.exam.speed_factor)
        added += max(real_max - base_max, 0)
    return added

@login_required
@require_http_methods(["GET"])
def recovery_compare(request, group_id):
    plans = list(
        RecoveryPlan.objects
        .filter(
            recovery_group_id=group_id,
            exam_period__user=request.user,
            status=RecoveryPlanStatus.PENDING,
        )
        .select_related("exam_period", "source_daily_plan")
        .prefetch_related("items__study_task__exam")
    )

    if not plans:
        raise Http404("복구안을 찾을 수 없습니다.")

    order = {RecoveryType.MAINTAIN_VOLUME: 0, RecoveryType.CORE_FOCUS: 1}
    plans.sort(key=lambda p: order.get(p.recovery_type, 99))

    exam_period = plans[0].exam_period
    source_daily_plan = plans[0].source_daily_plan
    future_capacity = get_future_available_capacity(exam_period, source_daily_plan.date)
    available_minutes = sum(item.available_minutes for item in future_capacity)

    totals_by_plan = {p.id: _plan_totals(p) for p in plans}
    axis_max = max(
        available_minutes,
        *(t["max_total"] for t in totals_by_plan.values()),
        1,
    ) * 1.15

    plan_contexts = [
        _build_plan_context(p, totals_by_plan[p.id], available_minutes, axis_max)
        for p in plans
    ]

    representative_id = next(
        (p.id for p in plans if p.recovery_type == RecoveryType.MAINTAIN_VOLUME),
        plans[0].id,
    )
    rep_totals = totals_by_plan[representative_id]
    rep_all_items = rep_totals["reschedule_items"] + rep_totals["excluded_items"]

    reason = {
        "headline": "오늘 계획한 학습을 다 마치지 못했습니다",
        "detail": f"{len(rep_all_items)}개 작업, {sum(i.remaining_minutes for i in rep_all_items)}분이 남아 계획을 다시 세워야 합니다.",
        "remaining_minutes": sum(i.remaining_minutes for i in rep_all_items),
        "remaining_count": len(rep_all_items),
        "speed_added_minutes": _speed_added_minutes(rep_all_items),
        "available_minutes": available_minutes,
        "available_days": len({item.date for item in future_capacity}),
    }

    # 미리보기에서 돌아온 경우 직전에 보고 있던 복구안을 그대로 선택 상태로 유지한다.
    # 없거나 이미 사라진 plan id면 기존 기본값(마지막 복구안)으로 되돌아간다.
    try:
        requested_selected_id = int(request.GET.get("selected"))
    except (TypeError, ValueError):
        requested_selected_id = None

    valid_ids = {p["id"] for p in plan_contexts}
    selected_plan_id = (
        requested_selected_id if requested_selected_id in valid_ids else plan_contexts[-1]["id"]
    )
    for p in plan_contexts:
        p["is_selected"] = (p["id"] == selected_plan_id)
    selected_plan = next(p for p in plan_contexts if p["id"] == selected_plan_id)

    return render(request, "planner/recovery_compare.html", {
        "exam_period": exam_period,
        "plans": plan_contexts,
        "selected_plan": selected_plan,
        "reason": reason,
    })

def _is_preview_exam_day(date, exam_dates, after_minutes):
    """
    해당 날짜가 어느 과목의 시험일이면서, 그 날 실제로 배치된 공부량이
    없을 때만 '시험'으로 표시한다. 다른 과목 공부가 있으면 bar를 그대로
    보여준다 (FE2 확인 완료 - 스케줄러가 다른 과목 시험일에는 배치를
    막지 않으므로).
    """
    return date in exam_dates and after_minutes == 0


@login_required
@require_http_methods(["GET"])
def recovery_preview(request, plan_id):
    recovery_plan = (
        RecoveryPlan.objects
        .filter(pk=plan_id, exam_period__user=request.user, status=RecoveryPlanStatus.PENDING)
        .select_related("exam_period", "source_daily_plan")
        .first()
    )
    if recovery_plan is None:
        raise Http404("복구안을 찾을 수 없습니다.")

    exam_period = recovery_plan.exam_period
    items = list(recovery_plan.items.select_related("study_task__exam"))
    reschedule_items = [i for i in items if i.action_type == RecoveryActionType.RESCHEDULE]
    excluded_items = [i for i in items if i.action_type == RecoveryActionType.EXCLUDE]

    start_date = max(
        recovery_plan.source_daily_plan.date + timedelta(days=1),
        timezone.localdate() + timedelta(days=1),
    )

    # before: 적용 직전 stale 검증(occupied)과 동일하게 DailyPlanItem.planned_minutes 합계로 계산
    existing_by_date = dict(
        DailyPlanItem.objects
        .filter(daily_plan__exam_period=exam_period, daily_plan__date__gte=start_date)
        .values("daily_plan__date")
        .annotate(total=Sum("planned_minutes"))
        .values_list("daily_plan__date", "total")
    )

    added_by_date = defaultdict(int)
    for item in reschedule_items:
        added_by_date[item.changed_date] += item.remaining_minutes

    available_by_date = dict(
        AvailableTime.objects.filter(
            exam_period=exam_period, date__gte=start_date
        ).values_list("date", "available_minutes")
    )

    exam_dates = set(exam_period.exams.values_list("exam_date", flat=True))
    exam_dates_in_range = {d for d in exam_dates if d >= start_date}

    all_dates = sorted(set(existing_by_date) | set(added_by_date) | exam_dates_in_range)

    days = []
    for d in all_dates:
        before_minutes = existing_by_date.get(d, 0)
        added_minutes = added_by_date.get(d, 0)
        after_minutes = before_minutes + added_minutes
        available_minutes = available_by_date.get(d, 0)

        scale = max(before_minutes, after_minutes, available_minutes, 1)
        days.append({
            "date": d,
            "is_exam_day": _is_preview_exam_day(d, exam_dates, after_minutes),
            "before_minutes": before_minutes,
            "before_pct": round(min(before_minutes / scale * 100, 100), 1),
            "before_over": before_minutes > available_minutes,
            "added_minutes": added_minutes,
            "after_minutes": after_minutes,
            "after_pct": round(min(after_minutes / scale * 100, 100), 1),
            "limit_pct": round(min(available_minutes / scale * 100, 100), 1),
        })

    keep_count = len(reschedule_items)
    keep_core_count = sum(
        1 for i in reschedule_items if i.study_task.depth == TaskDepth.CORE
    )
    move_count = sum(
        1 for i in reschedule_items if i.original_date != i.changed_date
    )

    exclude_count = len(excluded_items)
    exclude_minutes = sum(i.remaining_minutes for i in excluded_items)
    if exclude_count == 0:
        exclude_summary = ""
    elif exclude_count == 1:
        exclude_summary = excluded_items[0].study_task.title
    else:
        exclude_summary = f"{excluded_items[0].study_task.title} 외 {exclude_count - 1}건"

    type_label = "분량 유지형" if recovery_plan.recovery_type == RecoveryType.MAINTAIN_VOLUME else "핵심 집중형"

    context = {
        "exam_period": exam_period,
        "plan": {
            "id": recovery_plan.id,
            "type_label": type_label,
        },
        "preview": {
            "start_date": start_date,
            "days": days,
            "keep_count": keep_count,
            "keep_core_count": keep_core_count,
            "move_count": move_count,
            "exclude_count": exclude_count,
            "exclude_minutes": exclude_minutes,
            "exclude_summary": exclude_summary,
        },
        "compare_url": (
            reverse(
                "planner:recovery_compare",
                kwargs={"group_id": recovery_plan.recovery_group_id},
            )
            + f"?selected={recovery_plan.id}"
        ),
    }
    return render(request, "planner/recovery_result.html", context)


@login_required
@require_http_methods(["POST"])
def recovery_apply(request, plan_id):
    recovery_plan = get_object_or_404(
        RecoveryPlan.objects
        .select_related("exam_period", "source_daily_plan")
        .prefetch_related("items__study_task__exam"),
        pk=plan_id,
        exam_period__user=request.user,
    )

    try:
        apply_recovery_plan(recovery_plan)
    except RecoveryPlanAlreadyProcessedError:
        messages.error(request, "이미 처리된 복구안입니다.")
        return redirect("planner:dashboard")
    except RecoveryPlanStaleError:
        messages.error(
            request,
            "일정이나 가능시간이 변경되어 이 복구안을 적용할 수 없습니다. 복구안을 다시 확인해주세요.",
        )
        return redirect(
            "planner:recovery_compare",
            group_id=recovery_plan.recovery_group_id,
        )
    except RecoveryPlanInvalidDataError:
        messages.error(request, "복구안 데이터에 문제가 있어 적용할 수 없습니다.")
        return redirect(
            "planner:recovery_compare",
            group_id=recovery_plan.recovery_group_id,
        )

    except Exception:
        logger.exception(
            "복구안 적용 중 예상치 못한 오류 (plan_id=%s)", plan_id
        )
        messages.error(
            request, "복구안 적용 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요."
        )
        return redirect(
            "planner:recovery_compare",
            group_id=recovery_plan.recovery_group_id,
        )

    messages.success(request, "선택한 복구안이 일정에 적용되었습니다.")
    return redirect("planner:dashboard")