from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods
from django.utils import timezone

from exams.models import ExamPeriod, StudyTask, AvailableTime
from planner.models import DailyPlan
from planner.services.feasibility_checker import calculate_feasibility, POSSIBLE
from planner.services.schedule_generator import (
    generate_schedule,
    ScheduleAlreadyExistsError,
    UnallocatedTasksError,
    MismatchedExamPeriodError,
    DuplicateTaskAllocationError,
)


def _get_owned_exam_period(user, period_id):
    return get_object_or_404(ExamPeriod, id=period_id, user=user)


def _confirmed_tasks(exam_period):
    return StudyTask.objects.filter(
        exam__exam_period=exam_period, is_confirmed=True
    ).select_related('exam')


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

    context = {
        'exam_period': exam_period,
        'result': result,
        'total_min_minutes': result['required_min_minutes'],
        'total_max_minutes': result['required_recommended_minutes'],
        'total_available_minutes': result['available_minutes'],
        'task_count': len(tasks),
        'can_generate': is_ready and result['status'] == POSSIBLE,
        'readiness_error': readiness_error,
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