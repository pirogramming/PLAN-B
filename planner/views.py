import json
from django.http import JsonResponse
from planner.models import DailyPlan, DailyPlanItem, RecoveryPlan
from planner.services.progress_recorder import record_progress, FinalizedDailyPlanEditError
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from django.http import Http404
from core.choices import RecoveryActionType, RecoveryType
from planner.services.recovery import get_future_available_capacity
from planner.services.time_estimator import estimate_task_minutes
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods
from core.choices import ExamPeriodStatus, RecoveryPlanStatus, ProgressStatus
from django.utils import timezone
from django.urls import reverse
from exams.models import ExamPeriod, StudyTask, AvailableTime
from planner.services.feasibility_checker import calculate_feasibility, POSSIBLE
from planner.services.schedule_generator import (
    generate_schedule,
    ScheduleAlreadyExistsError,
    UnallocatedTasksError,
    MismatchedExamPeriodError,
    DuplicateTaskAllocationError,
)
from planner.services.progress_recorder import (
    finalize_daily_plan,
    DailyPlanAlreadyFinalizedError,
)

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
    과목별 카드 표시용 데이터. 가용시간은 시험기간 전체가 공유하는 구조라
    과목별 possible/risky/impossible 판정은 여기서 만들지 않는다.
    """
    subject_results = []

    for exam in exam_period.exams.all().order_by('exam_date'):
        subject_tasks = [task for task in tasks if task.exam_id == exam.id]

        subject_results.append({
            'exam_id': exam.id,
            'subject_name': exam.subject_name,
            'exam_date': exam.exam_date,
            'task_count': len(subject_tasks),
            'required_min_minutes': sum(
                task.estimated_min_minutes for task in subject_tasks
            ),
            'required_recommended_minutes': sum(
                task.estimated_max_minutes for task in subject_tasks
            ),
        })

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
    1단계 최소 버전: 시험기간 존재 여부, 계획 존재 여부, 오늘 할 일 개수만 보여준다.
    Fit Bar/과목별 요약/진행률 집계는 #51 머지 후 데이터 파이프라인이 갖춰지면 추가한다.
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

    context = {
        'exam_period': exam_period,
        'has_plan': True,
        'today': today,
        'today_count': today_count,
        'today_minutes': today_minutes,
        'pending_recovery': _get_pending_recovery(exam_period),
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
        'calendar_url': reverse('planner:dashboard'),
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

    return render(request, "planner/recovery_compare.html", {
        "exam_period": exam_period,
        "plans": plan_contexts,
        "reason": reason,
    })