import datetime
from django.utils import timezone
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_http_methods
from django.contrib import messages
from django.http import JsonResponse
from django.db import transaction
import logging
from django.core.exceptions import ValidationError
from core.choices import ExamPeriodStatus, MaterialStatus, MaterialType
from core.exceptions import AIAnalysisError
from planner.services.time_estimator import estimate_task_minutes
from django.utils.http import url_has_allowed_host_and_scheme
from .models import ExamPeriod, AvailableTime, Exam, StudyMaterial, StudyTask
from planner.models import DailyPlan, RecoveryPlan
from .forms import (
    ExamPeriodForm,
    AvailableTimeFormSet,
    ExamForm,
    StudyMaterialForm,
    StudyTaskForm,
    StudyTaskFormSet,
)
from .services.pdf_extractor import extract_text_from_pdf, PdfExtractionError
from .services.available_time_updater import (
    save_available_time_formset,
    blocked_date_messages,
    AvailableTimeEditRejected,
)
from .services.analysis_orchestrator import (
    analyze_and_estimate,   # 더 이상 view에서 직접 쓰지 않지만, 다른 곳에서 참조할 수 있어 import 유지 여부는 검토
    retry_analysis,
    claim_analysis_run,
    run_claimed_analysis,
    get_analysis_status,
    MAX_RETRY_COUNT,
    DuplicateAnalysisRequestError,
    AnalysisNotSupportedError,
    RetryLimitExceededError,
    AnalysisPipelineError,
    StaleAnalysisRunError,
)

from functools import wraps

logger = logging.getLogger(__name__)


def check_exam_period_locked_by_period_id(view_func):
    """period_id 기준: POST 요청 시 ExamPeriod를 Row Lock(select_for_update) 처리 후
    계획 존재 여부 검증 + view_func 실행까지 동일 트랜잭션/락 스코프 안에서 수행"""
    @wraps(view_func)
    def wrapped_view(request, period_id, *args, **kwargs):
        if request.method == 'POST':
            with transaction.atomic():
                period = get_object_or_404(
                    ExamPeriod.objects.select_for_update(),
                    id=period_id,
                    user=request.user
                )
                if DailyPlan.objects.filter(exam_period=period).exists():
                    messages.error(request, "이미 계획이 생성된 시험기간은 수정하거나 삭제할 수 없습니다.")
                    return redirect('exams:period_detail', period_id=period.id)
                return view_func(request, period_id, *args, **kwargs)
        return view_func(request, period_id, *args, **kwargs)
    return wrapped_view


def check_exam_period_locked_by_exam_id(view_func):
    @wraps(view_func)
    def wrapped_view(request, exam_id, *args, **kwargs):
        if request.method == 'POST':
            with transaction.atomic():
                exam = get_object_or_404(
                    Exam.objects.select_related('exam_period'),
                    id=exam_id,
                    exam_period__user=request.user
                )
                period = ExamPeriod.objects.select_for_update().get(id=exam.exam_period_id)
                if DailyPlan.objects.filter(exam_period=period).exists():
                    messages.error(request, "이미 계획이 생성된 시험기간의 과목은 수정하거나 삭제할 수 없습니다.")
                    return redirect('exams:period_detail', period_id=period.id)
                return view_func(request, exam_id, *args, **kwargs)
        return view_func(request, exam_id, *args, **kwargs)
    return wrapped_view


def check_exam_period_locked_by_material_id(view_func):
    @wraps(view_func)
    def wrapped_view(request, material_id, *args, **kwargs):
        if request.method == 'POST':
            with transaction.atomic():
                material = get_object_or_404(
                    StudyMaterial.objects.select_related('exam__exam_period'),
                    id=material_id,
                    exam__exam_period__user=request.user
                )
                period = ExamPeriod.objects.select_for_update().get(id=material.exam.exam_period_id)
                
                if DailyPlan.objects.filter(exam_period=period).exists():
                    messages.error(request, "이미 계획이 생성된 시험기간의 학습자료는 수정하거나 삭제할 수 없습니다.")
                    return redirect('exams:period_detail', period_id=period.id)

                # 💡 현재 처리 중인 자료(텍스트 추출 중 또는 AI 분석 중)인 경우 삭제/수정 차단
                if (
                    material.status == MaterialStatus.PROCESSING
                    or material.analysis_status == MaterialStatus.PROCESSING
                ):
                    messages.error(request, "현재 처리 중인 학습자료는 삭제하거나 수정할 수 없습니다.")
                    return redirect('exams:material_detail', material_id=material_id)

                return view_func(request, material_id, *args, **kwargs)
        return view_func(request, material_id, *args, **kwargs)
    return wrapped_view

def check_exam_period_not_locked_by_material_id(claim_func=None):
    """material_id 기준: ExamPeriod row lock 안에서
      1) 계획 존재 여부 검증
      2) (claim_func가 주어지면) material을 PROCESSING 등으로 원자적 선점
    까지 마친 뒤 락을 해제하고, view_func 자체(PDF 추출/AI 분석 같은
    장시간 외부 호출)는 트랜잭션·락 밖에서 실행한다.

    claim_func(material) -> (claimed, message, level, extra)
        claimed=False면 message/level(예: 'error'|'info')로 안내하고
        view_func를 호출하지 않은 채 material_detail로 리다이렉트한다.
        claimed=True면 extra는 view_func에 claim_extra 키워드 인자로
        그대로 전달된다 (예: 분석 run_id).

    이렇게 하면 planner.plan_generate()가 이후 같은 ExamPeriod를 잠갔을 때
    '이 material은 이미 PROCESSING 상태다'를 보고 계획 생성을 막을 수 있어,
    'AI 요청 → 락 해제 → 계획 생성 → 뒤늦게 선점/결과 반영'같은 역전이 불가능해진다.
    """
    def decorator(view_func):
        @wraps(view_func)
        def wrapped_view(request, material_id, *args, **kwargs):
            if request.method == 'POST':
                with transaction.atomic():
                    material = get_object_or_404(
                        StudyMaterial.objects.select_related('exam__exam_period'),
                        id=material_id,
                        exam__exam_period__user=request.user
                    )
                    period = ExamPeriod.objects.select_for_update().get(id=material.exam.exam_period_id)
                    if DailyPlan.objects.filter(exam_period=period).exists():
                        messages.error(request, "이미 계획이 생성된 시험기간의 학습자료는 수정하거나 삭제할 수 없습니다.")
                        return redirect('exams:period_detail', period_id=period.id)

                    if claim_func is not None:
                        claimed, message, level, extra = claim_func(material)
                        if not claimed:
                            getattr(messages, level)(request, message)
                            return redirect('exams:material_detail', material_id=material.id)
                        kwargs['claim_extra'] = extra
                # atomic 블록 종료 → ExamPeriod 락 해제.
                # 이 시점에 이미 material은 선점되어 있으므로 이후 시작되는
                # plan_generate()와 안전하게 직렬화된다.
            return view_func(request, material_id, *args, **kwargs)
        return wrapped_view
    return decorator

# =====================================================================
# 시험기간 목록 (exams:period_list) 
# =====================================================================
@login_required
@require_http_methods(["GET"])
def period_list(request):
    periods = ExamPeriod.objects.filter(user=request.user)
    ongoing_periods = periods.exclude(
        status__in=[ExamPeriodStatus.COMPLETED, ExamPeriodStatus.ARCHIVED]
    )
    completed_periods = periods.filter(
        status__in=[ExamPeriodStatus.COMPLETED, ExamPeriodStatus.ARCHIVED]
    )
    context = {
        'ongoing_periods': ongoing_periods,
        'completed_periods': completed_periods,
        'has_periods': periods.exists(),
    }
    return render(request, 'exams/period_list.html', context)


# =====================================================================
# 시험기간 생성 (exams:period_create) 
# =====================================================================
@login_required
@require_http_methods(["GET", "POST"])
def period_create(request):
    """
    생성 전용. 수정은 period_update가 따로 담당.
    - active 시험기간 1개 제한 (MVP 정책)
    - 생성 성공 시 '시험기간 상세'로 이동
    - start_date~end_date 범위의 AvailableTime을 0분으로 미리 채워둠
    """
    if request.method == 'POST':
        form = ExamPeriodForm(request.POST)
        if form.is_valid():
            if ExamPeriod.objects.filter(
                user=request.user, status=ExamPeriodStatus.ACTIVE
            ).exists():
                messages.error(request, "이미 진행 중인 시험기간이 있습니다. 기존 시험기간을 완료하거나 보관 처리한 뒤 새로 만들어주세요.")
                return render(request, 'exams/period_form.html', {'form': form})

            period = form.save(commit=False)
            period.user = request.user
            period.status = ExamPeriodStatus.ACTIVE
            period.save()

            curr_date = period.start_date
            while curr_date <= period.end_date:
                AvailableTime.objects.get_or_create(
                    exam_period=period, date=curr_date, defaults={'available_minutes': 0}
                )
                curr_date += datetime.timedelta(days=1)

            return redirect('exams:period_detail', period_id=period.id)
    else:
        form = ExamPeriodForm()

    return render(request, 'exams/period_form.html', {'form': form})


# =====================================================================
# 시험기간 수정 (exams:period_update) 
# =====================================================================
@login_required
@check_exam_period_locked_by_period_id
@require_http_methods(["GET", "POST"])
def period_update(request, period_id):
    period = get_object_or_404(ExamPeriod, id=period_id, user=request.user)

    if request.method == 'POST':
        form = ExamPeriodForm(request.POST, instance=period)
        if form.is_valid():
            with transaction.atomic():
                updated = form.save()

                # 축소된 경우: 새 범위(start_date~end_date) 밖의 AvailableTime 삭제
                AvailableTime.objects.filter(exam_period=updated).exclude(
                    date__range=(updated.start_date, updated.end_date)
                ).delete()

                # 확장된 경우: 새로 생긴 날짜만 0분으로 채움 (기존 값은 안 건드림)
                curr_date = updated.start_date
                while curr_date <= updated.end_date:
                    AvailableTime.objects.get_or_create(
                        exam_period=updated, date=curr_date, defaults={'available_minutes': 0}
                    )
                    curr_date += datetime.timedelta(days=1)

            return redirect('exams:period_detail', period_id=period.id)
    else:
        form = ExamPeriodForm(instance=period)

    return render(request, 'exams/period_form.html', {'form': form, 'period': period})


# =====================================================================
# 시험기간 삭제 (exams:period_delete)
# =====================================================================
@login_required
@require_http_methods(["POST"])
def period_delete(request, period_id):
    period = get_object_or_404(ExamPeriod, id=period_id, user=request.user)
    
    # 1. delete() 실행 전, 알림 메시지에 쓸 title 변수 추출 (안전성 보장)
    period_title = period.title

    with transaction.atomic():
        # 2. PROTECT 조건 방해 요인인 DailyPlan / RecoveryPlan 선-삭제
        # (CASCADE에 의해 DailyPlanItem, ProgressLog, RecoveryPlanItem이 함께 정리됨)
        DailyPlan.objects.filter(exam_period=period).delete()
        RecoveryPlan.objects.filter(exam_period=period).delete()
        
        # 3. ExamPeriod 삭제 (Exam, StudyTask, AvailableTime, StudyMaterial CASCADE 삭제)
        period.delete()

    messages.success(request, f"'{period_title}' 시험기간과 관련 학습 계획이 모두 삭제되었습니다.")
    return redirect('exams:period_list')


# =====================================================================
# 시험기간 상세 (exams:period_detail) 
# =====================================================================
@login_required
@require_http_methods(["GET"])
def period_detail(request, period_id):
    period = get_object_or_404(ExamPeriod, id=period_id, user=request.user)
    exams = period.exams.all()
    available_times = period.available_times.all()
    context = {'period': period, 'exams': exams, 'available_times': available_times}
    return render(request, 'exams/period_detail.html', context)


# =====================================================================
# 시험기간 관리 (exams:period_manage)
# 계획이 이미 생성된 시험기간용 화면. period_detail과 달리 과목 추가/수정/
# 삭제·시험범위 등록은 여기서 할 수 없다 (계획이 그 데이터를 기준으로 이미
# 배치돼 있어서, 여기서 바꾸면 계획과 어긋난다) — 가능시간 수정, 학습작업
# 확인, 시험기간 종료만 가능하다.
# =====================================================================
@login_required
@require_http_methods(["GET"])
def period_manage(request, period_id):
    period = get_object_or_404(ExamPeriod, id=period_id, user=request.user)
    if not DailyPlan.objects.filter(exam_period=period).exists():
        return redirect('exams:period_detail', period_id=period.id)
    exams = period.exams.all()
    available_times = period.available_times.all()
    context = {'period': period, 'exams': exams, 'available_times': available_times}
    return render(request, 'exams/period_manage.html', context)


# =====================================================================
# 시험기간 관리 - 가능시간 수정 (exams:period_manage_available_time)
# available_time_update의 축소판. feasibility 등 다른 화면은 여전히
# available_time_update(다음 버튼, next 파라미터)를 그대로 쓰고, 이 화면은
# period_manage 전용이라 저장 버튼 하나만 있고 항상 period_manage로 돌아간다.
# =====================================================================
@login_required
@require_http_methods(["GET", "POST"])
def period_manage_available_time(request, period_id):
    period = get_object_or_404(ExamPeriod, id=period_id, user=request.user)
    if not DailyPlan.objects.filter(exam_period=period).exists():
        return redirect('exams:period_detail', period_id=period.id)
    queryset = AvailableTime.objects.filter(exam_period=period).order_by('date')

    if request.method == 'POST':
        formset = AvailableTimeFormSet(request.POST, queryset=queryset)
        if formset.is_valid():
            try:
                save_available_time_formset(formset, exam_period=period)
            except AvailableTimeEditRejected as exc:
                # BaseFormSet에는 Form.add_error() 같은 공개 API가 없어서,
                # non_form_errors()가 실제로 읽는 내부 리스트에 직접 추가한다
                # (Django formset.full_clean()이 내부적으로 쓰는 것과 동일한 패턴).
                for msg in blocked_date_messages(exc.blocked_dates):
                    formset._non_form_errors.append(ValidationError(msg))
                    messages.error(request, msg)
            else:
                messages.success(request, "가용 시간이 성공적으로 저장되었습니다.")
                return redirect('exams:period_manage', period_id=period.id)
    else:
        formset = AvailableTimeFormSet(queryset=queryset)

    return render(request, 'exams/period_manage_available_time.html', {
        'formset': formset,
        'period': period,
    })


# =====================================================================
# 시험기간 관리 - 학습작업 확인 (exams:period_manage_task_view)
# task_review의 읽기 전용 버전. 계획이 이미 생성된 뒤라 작업 추가/수정/삭제·
# 확정은 계획과 어긋날 수 있어서 막고, 내용 확인만 가능하다.
# =====================================================================
@login_required
@require_http_methods(["GET"])
def period_manage_task_view(request, exam_id):
    exam = get_object_or_404(Exam, id=exam_id, exam_period__user=request.user)
    if not DailyPlan.objects.filter(exam_period=exam.exam_period_id).exists():
        return redirect('exams:period_detail', period_id=exam.exam_period_id)
    tasks = StudyTask.objects.filter(exam=exam).order_by('order', 'id')
    return render(request, 'exams/period_manage_task_view.html', {
        'exam': exam,
        'tasks': tasks,
    })


# =====================================================================
# 과목 추가 (exams:subject_create) 
# =====================================================================
@login_required
@check_exam_period_locked_by_period_id
@require_http_methods(["GET", "POST"])
def subject_create(request, period_id):
    period = get_object_or_404(ExamPeriod, id=period_id, user=request.user)

    if request.method == 'POST':
        form = ExamForm(request.POST, exam_period=period)
        if form.is_valid():
            exam = form.save(commit=False)
            exam.exam_period = period
            exam.save()
            return redirect('exams:period_detail', period_id=period.id)
    else:
        form = ExamForm(exam_period=period)

    return render(request, 'exams/subject_form.html', {'form': form, 'period': period})


# =====================================================================
# 과목 수정 (exams:subject_update) 
# =====================================================================
@login_required
@check_exam_period_locked_by_period_id
@require_http_methods(["GET", "POST"])
def subject_update(request, period_id, exam_id):
    period = get_object_or_404(
        ExamPeriod,
        id=period_id,
        user=request.user,
    )

    exam = get_object_or_404(
        Exam,
        id=exam_id,
        exam_period=period,
    )

    if request.method == "POST":
        form = ExamForm(
            request.POST,
            instance=exam,
            exam_period=period,
        )

        if form.is_valid():
            form.save()

            return redirect(
                "exams:period_detail",
                period_id=period.id,
            )
    else:
        form = ExamForm(
            instance=exam,
            exam_period=period,
        )

    return render(
        request,
        "exams/subject_form.html",
        {
            "form": form,
            "period": period,
            "exam": exam,
        },
    )

# =====================================================================
# 과목 삭제 (exams:subject_delete) 
# =====================================================================
@login_required
@check_exam_period_locked_by_period_id
@require_http_methods(["POST"])
def subject_delete(request, period_id, exam_id):
    period = get_object_or_404(
        ExamPeriod,
        id=period_id,
        user=request.user,
    )

    exam = get_object_or_404(
        Exam,
        id=exam_id,
        exam_period=period,
    )

    subject_name = exam.subject_name
    exam.delete()

    messages.success(
        request,
        f"'{subject_name}' 과목이 삭제되었습니다.",
    )

    return redirect(
        "exams:period_detail",
        period_id=period.id,
    )

# =====================================================================
# 가능시간 입력 (exams:available_time_update)
# =====================================================================
@login_required
@check_exam_period_locked_by_period_id
@require_http_methods(["GET", "POST"])
def available_time_update(request, period_id):
    period = get_object_or_404(
        ExamPeriod,
        id=period_id,
        user=request.user,
    )

    queryset = AvailableTime.objects.filter(
        exam_period=period
    ).order_by("date")

    # ================================================================
    # POST
    # ================================================================
    if request.method == "POST":

        formset = AvailableTimeFormSet(
            request.POST,
            queryset=queryset,
        )

        next_url = (
            request.POST.get("next")
            or request.GET.get("next")
            or request.META.get("HTTP_REFERER", "")
        )

        if not formset.is_valid():
            return render(
                request,
                "exams/available_time_form.html",
                {
                    "formset": formset,
                    "period": period,
                    "next": next_url,
                },
            )

        try:
            save_available_time_formset(formset, exam_period=period)
        except AvailableTimeEditRejected as exc:
            for msg in blocked_date_messages(exc.blocked_dates):
                # BaseFormSet에는 Form.add_error() 같은 공개 API가 없어서,
                # non_form_errors()가 실제로 읽는 내부 리스트에 직접 추가한다
                # (Django formset.full_clean()이 내부적으로 쓰는 것과 동일한 패턴).
                formset._non_form_errors.append(ValidationError(msg))
                messages.error(request, msg)

            return render(
                request,
                "exams/available_time_form.html",
                {
                    "formset": formset,
                    "period": period,
                    "next": next_url,
                },
            )

        messages.success(request, "가용 시간이 성공적으로 저장되었습니다.")

        if next_url and url_has_allowed_host_and_scheme(
            next_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            return redirect(next_url)

        return redirect("exams:period_detail", period_id=period.id)

    # ================================================================
    # GET
    # ================================================================
    formset = AvailableTimeFormSet(
        queryset=queryset,
    )

    next_url = (
        request.GET.get("next")
        or request.META.get("HTTP_REFERER", "")
    )

    return render(
        request,
        "exams/available_time_form.html",
        {
            "formset": formset,
            "period": period,
            "next": next_url,
        },
    )

# =====================================================================
# 자료 등록 (exams:material_create) 
# =====================================================================
@login_required
@check_exam_period_locked_by_exam_id
@require_http_methods(["GET", "POST"])
def material_create(request, exam_id):
    exam = get_object_or_404(
        Exam,
        id=exam_id,
        exam_period__user=request.user,
    )

    if request.method == "POST":
        form = StudyMaterialForm(
            request.POST,
            request.FILES,
        )

        if form.is_valid():
            material = form.save(commit=False)
            material.exam = exam

            if material.material_type == MaterialType.TEXT:
                material.status = MaterialStatus.COMPLETED

            material.save()

            return redirect(
                "exams:material_detail",
                material_id=material.id,
            )
    else:
        form = StudyMaterialForm()

    return render(
        request,
        "exams/material_form.html",
        {
            "form": form,
            "exam": exam,
        },
    )

# =====================================================================
# 자료 상세 (exams:material_detail)
# =====================================================================
@login_required
@require_http_methods(["GET"])
def material_detail(request, material_id):
    material = get_object_or_404(StudyMaterial, id=material_id, exam__exam_period__user=request.user)
    tasks = material.study_tasks.all()
    return render(request, 'exams/material_detail.html', {'material': material, 'tasks': tasks})


# =====================================================================
# PDF 텍스트 추출 (exams:material_extract) 
# =====================================================================
def _claim_material_for_extraction(material):
    if material.material_type != MaterialType.PDF:
        return False, "PDF 자료만 텍스트 추출이 가능합니다.", "error", None
    if not material.file:
        return False, "첨부된 PDF 파일이 없습니다.", "error", None

    updated = StudyMaterial.objects.filter(pk=material.pk).exclude(
        status=MaterialStatus.PROCESSING
    ).exclude(
        analysis_status__in=[MaterialStatus.PROCESSING, MaterialStatus.COMPLETED]
    ).update(status=MaterialStatus.PROCESSING, error_message=None)

    if not updated:
        material.refresh_from_db(fields=['status', 'analysis_status'])
        if material.status == MaterialStatus.PROCESSING:
            return False, "이미 PDF 텍스트를 추출 중인 자료입니다.", "info", None
        if material.analysis_status == MaterialStatus.PROCESSING:
            return False, "AI 분석이 진행 중인 자료는 다시 추출할 수 없습니다.", "error", None
        return False, "이미 AI 분석이 완료된 자료입니다. 다시 추출하려면 먼저 작업 검토 화면에서 확인해주세요.", "error", None

    return True, None, None, None


@login_required
@check_exam_period_not_locked_by_material_id(claim_func=_claim_material_for_extraction)
@require_http_methods(["POST"])
def material_extract(request, material_id, claim_extra=None):
    material = get_object_or_404(
        StudyMaterial, id=material_id, exam__exam_period__user=request.user
    )
    previous_extracted_text = material.extracted_text
    # material_type / file / status 선점은 claim_func가 락 안에서 이미 끝냈으므로
    # 여기서는 바로 추출을 진행한다.

    try:
        extracted = extract_text_from_pdf(material.file)
    except PdfExtractionError as e:
        material.status = MaterialStatus.FAILED
        material.error_message = str(e)
        material.save(update_fields=['status', 'error_message'])
        messages.error(request, "PDF 텍스트 추출에 실패했습니다.")
        return redirect('exams:material_detail', material_id=material.id)

    if not extracted:
        material.status = MaterialStatus.FAILED
        material.error_message = "텍스트를 추출할 수 없습니다. 스캔 이미지 PDF는 지원하지 않습니다."
        material.save(update_fields=['status', 'error_message'])
        messages.warning(request, "텍스트를 추출하지 못했습니다. 스캔 이미지 PDF일 수 있어요.")
        return redirect('exams:material_detail', material_id=material.id)

    material.status = MaterialStatus.COMPLETED
    material.extracted_text = extracted
    material.error_message = None

    if extracted != previous_extracted_text:
        material.analysis_status = MaterialStatus.PENDING
        material.analysis_error_message = None
        material.analysis_retry_count = 0

    material.save(update_fields=[
        'status', 'extracted_text', 'error_message',
        'analysis_status', 'analysis_error_message', 'analysis_retry_count',
    ])
    messages.success(request, "PDF 텍스트 추출이 완료되었습니다.")
    return redirect('exams:material_detail', material_id=material.id)


# =====================================================================
# 자료 삭제 (exams:material_delete) 
# =====================================================================
@login_required
@check_exam_period_locked_by_material_id
@require_http_methods(["POST"])
def material_delete(request, material_id):
    material = get_object_or_404(
        StudyMaterial,
        id=material_id,
        exam__exam_period__user=request.user,
    )

    exam = material.exam
    period_id = exam.exam_period_id

    material.delete()

    messages.success(
        request,
        "학습자료가 삭제되었습니다.",
    )

    return redirect(
        "exams:period_detail",
        period_id=period_id,
    )

# =====================================================================
# AI 분석 실행 (exams:material_analyze) - E-AI-01
# =====================================================================
def _claim_material_for_analysis(material):
    if material.status != MaterialStatus.COMPLETED:
        return False, "텍스트 추출이 완료된 자료만 AI 분석을 시작할 수 있습니다.", "error", None
    try:
        run_id = claim_analysis_run(material, is_retry=False)
    except DuplicateAnalysisRequestError:
        return False, "이미 분석 중이거나 처리된 자료입니다.", "info", None
    return True, None, None, run_id


@login_required
@check_exam_period_not_locked_by_material_id(claim_func=_claim_material_for_analysis)
@require_http_methods(["POST"])
def material_analyze(request, material_id, claim_extra=None):
    material = get_object_or_404(
        StudyMaterial, id=material_id, exam__exam_period__user=request.user
    )

    try:
        run_claimed_analysis(material, claim_extra)
        messages.success(request, "AI 분석이 완료되었습니다.")
    except StaleAnalysisRunError:
        logger.info(f"AI 분석 실행이 완료 직전 다른 실행에 선점됨 (material_id={material_id})")
        messages.info(request, "다른 요청이 먼저 이 자료를 처리했습니다. 최신 상태를 다시 확인해주세요.")
        return redirect('exams:material_detail', material_id=material.id)
    except (AIAnalysisError, AnalysisPipelineError):
        messages.error(request, "AI 분석에 실패했습니다. 다시 시도하거나 직접 작업을 추가해주세요.")
        return redirect('exams:material_detail', material_id=material.id)
    except Exception:
        logger.exception(f"AI 분석 실행 중 예기치 못한 시스템 오류 발생 (material_id={material_id})")
        messages.error(request, "AI 분석 처리 중 알 수 없는 시스템 오류가 발생했습니다.")
        return redirect('exams:material_detail', material_id=material.id)

    return redirect('exams:task_review', exam_id=material.exam_id)



# =====================================================================
# AI 분석 재시도 (exams:material_retry_analyze) - E-AI-03
# =====================================================================
def _claim_material_for_retry(material):
    try:
        run_id = claim_analysis_run(material, is_retry=True)
    except DuplicateAnalysisRequestError:
        return False, "이미 분석 중인 자료입니다.", "info", None
    except AnalysisNotSupportedError as e:
        return False, str(e), "error", None
    except RetryLimitExceededError as e:
        return False, str(e), "error", None
    return True, None, None, run_id


@login_required
@check_exam_period_not_locked_by_material_id(claim_func=_claim_material_for_retry)
@require_http_methods(["POST"])
def material_retry_analyze(request, material_id, claim_extra=None):
    material = get_object_or_404(
        StudyMaterial, id=material_id, exam__exam_period__user=request.user
    )

    try:
        run_claimed_analysis(material, claim_extra)
    except StaleAnalysisRunError:
        logger.info(f"AI 재시도 실행이 완료 직전 다른 실행에 선점됨 (material_id={material_id})")
        messages.info(request, "다른 요청이 먼저 이 자료를 처리했습니다. 최신 상태를 다시 확인해주세요.")
        return redirect('exams:material_detail', material_id=material.id)
    except (AIAnalysisError, AnalysisPipelineError):
        messages.error(request, "재시도한 AI 분석도 실패했습니다.")
        return redirect('exams:material_detail', material_id=material.id)
    except Exception:
        logger.exception(f"AI 분석 재시도 중 예기치 못한 시스템 오류 발생 (material_id={material_id})")
        messages.error(request, "AI 분석 재시도 처리 중 알 수 없는 시스템 오류가 발생했습니다.")
        return redirect('exams:material_detail', material_id=material.id)

    messages.success(request, "AI 분석이 완료되었습니다.")
    return redirect('exams:task_review', exam_id=material.exam_id)

# =====================================================================
# AI 분석 상태 조회 (exams:material_analysis_status) - E-AI-02
# =====================================================================
@login_required
@require_http_methods(["GET"])
def material_analysis_status(request, material_id):
    """
    E-AI-02: AI 분석 및 텍스트 추출 진행 상태 조회 (폴링용 JSON 엔드포인트).
    """
    material = get_object_or_404(
        StudyMaterial, id=material_id, exam__exam_period__user=request.user
    )

    # get_analysis_status(material) 호출 복원
    analysis_data = get_analysis_status(material)

    extraction_status = material.status
    extraction_error = material.error_message
    
    analysis_status = analysis_data["status"]
    analysis_error = analysis_data["error_message"]
    retry_count = analysis_data["retry_count"]
    retry_remaining = analysis_data["retry_remaining"]
    # 재시도 버튼을 켜고 끄면 되도록 서버가 판단한 결과를 그대로 내려준다.
    can_retry = analysis_data["can_retry"]
    retry_after_seconds = analysis_data["retry_after_seconds"]
    # 리뷰 반영(#84): stage="ANALYZING"만으로는 "정상적으로 진행 중"인지
    # "5분 넘게 멈춘 좀비인데 재시도 횟수까지 소진돼 더 이상 손쓸 수 없는 상태"인지
    # FE가 구분할 수 없었다. is_stale을 같이 내려줘서, is_stale=True인데
    # can_retry=False면 "재시도 불가, 직접 작업 추가 안내"로 구분할 수 있게 한다.
    is_stale = analysis_data["is_stale"]

    # 1. 전체 stage 판정 로직 (작성하신 추출 우선 stage 판정 유지)
    failed_stage = None

    # ① 재추출 진행 중이면 이전 분석 실패보다 최우선으로 "EXTRACTING"
    if extraction_status == MaterialStatus.PROCESSING:
        stage = "EXTRACTING"

    # ② 추출 자체가 실패한 경우
    elif extraction_status == MaterialStatus.FAILED:
        stage = "FAILED"
        failed_stage = "EXTRACTION"

    # ③ 분석 진행 중인 경우
    elif analysis_status == MaterialStatus.PROCESSING:
        stage = "ANALYZING"

    # ④ 분석이 실패한 경우
    elif analysis_status == MaterialStatus.FAILED:
        stage = "FAILED"
        failed_stage = "ANALYSIS"

    # ⑤ 둘 다 완료된 경우
    elif (
        extraction_status == MaterialStatus.COMPLETED
        and analysis_status == MaterialStatus.COMPLETED
    ):
        stage = "COMPLETED"

    # ⑥ 아무것도 안 한 PENDING 상태
    else:
        stage = "PENDING"

    # 2. 약속된 JSON 응답 스펙 반환
    return JsonResponse({
        "stage": stage,
        "extraction_status": extraction_status,
        "extraction_error_message": extraction_error,
        "analysis_status": analysis_status,
        "analysis_error_message": analysis_error,
        "failed_stage": failed_stage,
        "retry_count": retry_count,
        "retry_remaining": retry_remaining,
        "can_retry": can_retry,
        "retry_after_seconds": retry_after_seconds,
        "is_stale": is_stale,
        "study_material_id": material.id,
        "exam_id": material.exam_id,
    })


# =====================================================================
# AI 작업 검토 (exams:task_review) 
# =====================================================================
@login_required
@check_exam_period_locked_by_exam_id
@require_http_methods(["GET", "POST"])
def task_review(request, exam_id):
    exam = get_object_or_404(
        Exam,
        id=exam_id,
        exam_period__user=request.user,
    )

    queryset = StudyTask.objects.filter(exam=exam)

    if request.method == "POST":
        action = request.POST.get("action")
        formset = StudyTaskFormSet(
            request.POST,
            queryset=queryset,
        )

        if formset.is_valid():
            instances = formset.save(commit=False)

            for instance in instances:
                instance.exam = exam
                instance.is_user_modified = True

                estimated_min, estimated_max = estimate_task_minutes(
                    task_type=instance.task_type,
                    difficulty=instance.difficulty,
                    speed_factor=exam.speed_factor,
                )

                instance.estimated_min_minutes = estimated_min
                instance.estimated_max_minutes = estimated_max

                instance.save()

            for obj in formset.deleted_objects:
                obj.delete()

            if action in ("confirm", "confirm_and_next"):
                exam.study_tasks.filter(
                    is_confirmed=False
                ).update(is_confirmed=True)

                return redirect(
                    "planner:feasibility",
                    period_id=exam.exam_period_id,
                )

            return redirect(
                "exams:task_review",
                exam_id=exam.id,
            )

    else:
        formset = StudyTaskFormSet(
            queryset=queryset,
        )

    return render(
        request,
        "exams/task_review.html",
        {
            "formset": formset,
            "exam": exam,
        },
    )


# =====================================================================
# 학습 작업 직접 추가 (exams:task_create) 
# =====================================================================
@login_required
@check_exam_period_locked_by_exam_id
@require_http_methods(["GET", "POST"])
def study_task_create(request, exam_id):
    exam = get_object_or_404(
        Exam,
        id=exam_id,
        exam_period__user=request.user,
    )

    if request.method == "POST":
        form = StudyTaskForm(request.POST)

        if form.is_valid():
            task = form.save(commit=False)
            task.exam = exam

            estimated_min, estimated_max = estimate_task_minutes(
                task_type=task.task_type,
                difficulty=task.difficulty,
                speed_factor=exam.speed_factor,
            )

            task.estimated_min_minutes = estimated_min
            task.estimated_max_minutes = estimated_max
            task.is_user_modified = True
            task.save()

            return redirect(
                "exams:task_review",
                exam_id=exam.id,
            )
    else:
        form = StudyTaskForm()

    return render(
        request,
        "exams/task_form.html",
        {
            "form": form,
            "exam": exam,
        },
    )

# =====================================================================
# 학습 작업 확정 (exams:task_confirm) 
# =====================================================================
@login_required
@check_exam_period_locked_by_exam_id
@require_http_methods(["POST"])
def study_task_confirm(request, exam_id):
    exam = get_object_or_404(
        Exam,
        id=exam_id,
        exam_period__user=request.user,
    )

    tasks = StudyTask.objects.filter(
        exam=exam,
        is_confirmed=False,
    )

    if not tasks.exists():
        messages.warning(
            request,
            "확정할 학습 작업이 없습니다. 먼저 작업을 검토해주세요.",
        )

        return redirect(
            "exams:task_review",
            exam_id=exam.id,
        )

    for task in tasks:
        task.is_confirmed = True
        task.save()

    messages.success(
        request,
        f"{tasks.count()}개 학습 작업이 확정되었습니다.",
    )

    return redirect(
        "planner:feasibility",
        period_id=exam.exam_period.id,
    )