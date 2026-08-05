import datetime
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_http_methods
from django.contrib import messages
from django.http import JsonResponse
from django.db import transaction

from core.choices import ExamPeriodStatus, MaterialStatus, MaterialType
from core.exceptions import AIAnalysisError
from planner.services.time_estimator import estimate_task_minutes

from .models import ExamPeriod, AvailableTime, Exam, StudyMaterial, StudyTask
from .forms import (
    ExamPeriodForm,
    AvailableTimeFormSet,
    ExamForm,
    StudyMaterialForm,
    StudyTaskForm,
    StudyTaskFormSet,
)
from .services.pdf_extractor import extract_text_from_pdf, PdfExtractionError
from .services.analysis_orchestrator import (
    analyze_and_estimate,
    retry_analysis,
    get_analysis_status,
    MAX_RETRY_COUNT,
    DuplicateAnalysisRequestError,
    AnalysisNotSupportedError,
    RetryLimitExceededError,
    AnalysisPipelineError,
)


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
    title = period.title
    period.delete()
    messages.success(request, f"'{title}' 시험기간이 삭제되었습니다.")
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
# 과목 추가 (exams:subject_create) 
# =====================================================================
@login_required
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
@require_http_methods(["GET", "POST"])
def subject_update(request, period_id, exam_id):
    period = get_object_or_404(ExamPeriod, id=period_id, user=request.user)
    exam = get_object_or_404(Exam, id=exam_id, exam_period=period)

    if request.method == 'POST':
        form = ExamForm(request.POST, instance=exam, exam_period=period)
        if form.is_valid():
            form.save()
            return redirect('exams:period_detail', period_id=period.id)
    else:
        form = ExamForm(instance=exam, exam_period=period)

    return render(request, 'exams/subject_form.html', {'form': form, 'period': period, 'exam': exam})


# =====================================================================
# 과목 삭제 (exams:subject_delete) 
# =====================================================================
@login_required
@require_http_methods(["POST"])
def subject_delete(request, period_id, exam_id):
    period = get_object_or_404(ExamPeriod, id=period_id, user=request.user)
    exam = get_object_or_404(Exam, id=exam_id, exam_period=period)
    subject_name = exam.subject_name
    exam.delete()
    messages.success(request, f"'{subject_name}' 과목이 삭제되었습니다.")
    return redirect('exams:period_detail', period_id=period.id)


# =====================================================================
# 가능시간 입력 (exams:available_time_update) 
# =====================================================================
@login_required
@require_http_methods(["GET", "POST"])
def available_time_update(request, period_id):
    period = get_object_or_404(ExamPeriod, id=period_id, user=request.user)
    queryset = AvailableTime.objects.filter(exam_period=period).order_by('date')

    if request.method == 'POST':
        formset = AvailableTimeFormSet(request.POST, queryset=queryset)
        if formset.is_valid():
            instances = formset.save(commit=False)
            for instance in instances:
                instance.exam_period = period
                instance.save()
            return redirect('exams:period_detail', period_id=period.id)
    else:
        formset = AvailableTimeFormSet(queryset=queryset)

    return render(request, 'exams/available_time_form.html', {'formset': formset, 'period': period})


# =====================================================================
# 자료 등록 (exams:material_create) 
# =====================================================================
@login_required
@require_http_methods(["GET", "POST"])
def material_create(request, exam_id):
    exam = get_object_or_404(Exam, id=exam_id, exam_period__user=request.user)

    if request.method == 'POST':
        form = StudyMaterialForm(request.POST, request.FILES)
        if form.is_valid():
            material = form.save(commit=False)
            material.exam = exam
            if material.material_type == MaterialType.TEXT:
                material.status = MaterialStatus.COMPLETED
            material.save()
            return redirect('exams:material_detail', material_id=material.id)
    else:
        form = StudyMaterialForm()

    return render(request, 'exams/material_form.html', {'form': form, 'exam': exam})


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
@login_required
@require_http_methods(["POST"])
def material_extract(request, material_id):
    material = get_object_or_404(
        StudyMaterial, id=material_id, exam__exam_period__user=request.user
    )
    previous_extracted_text = material.extracted_text

    if material.material_type != MaterialType.PDF:
        messages.error(request, "PDF 자료만 텍스트 추출이 가능합니다.")
        return redirect('exams:material_detail', material_id=material.id)

    if not material.file:
        messages.error(request, "첨부된 PDF 파일이 없습니다.")
        return redirect('exams:material_detail', material_id=material.id)

    updated_count = StudyMaterial.objects.filter(pk=material.pk).exclude(
        status=MaterialStatus.PROCESSING
    ).exclude(
        analysis_status__in=[MaterialStatus.PROCESSING, MaterialStatus.COMPLETED]
    ).update(status=MaterialStatus.PROCESSING, error_message=None)

    if not updated_count:
        material.refresh_from_db(fields=['status', 'analysis_status'])
        if material.status == MaterialStatus.PROCESSING:
            messages.info(request, "이미 PDF 텍스트를 추출 중인 자료입니다.")
        elif material.analysis_status == MaterialStatus.PROCESSING:
            messages.error(request, "AI 분석이 진행 중인 자료는 다시 추출할 수 없습니다.")
        else:
            messages.error(
                request,
                "이미 AI 분석이 완료된 자료입니다. 다시 추출하려면 먼저 작업 검토 "
                "화면에서 확인해주세요.",
            )
        return redirect('exams:material_detail', material_id=material.id)

    material.refresh_from_db(fields=['status', 'error_message'])

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
@require_http_methods(["POST"])
def material_delete(request, material_id):
    material = get_object_or_404(StudyMaterial, id=material_id, exam__exam_period__user=request.user)
    exam = material.exam
    period_id = exam.exam_period_id
    material.delete()
    messages.success(request, "학습자료가 삭제되었습니다.")
    return redirect('exams:period_detail', period_id=period_id)


# =====================================================================
# AI 분석 실행 (exams:material_analyze) - E-AI-01
# =====================================================================
@login_required
@require_http_methods(["POST"])
def material_analyze(request, material_id):
    material = get_object_or_404(
        StudyMaterial, id=material_id, exam__exam_period__user=request.user
    )

    if material.status != MaterialStatus.COMPLETED:
        messages.error(request, "텍스트 추출이 완료된 자료만 AI 분석을 시작할 수 있습니다.")
        return redirect('exams:material_detail', material_id=material.id)

    try:
        analyze_and_estimate(material)
    except DuplicateAnalysisRequestError:
        messages.info(request, "이미 분석 중이거나 처리된 자료입니다.")
        return redirect('exams:material_detail', material_id=material.id)
    except (AIAnalysisError, AnalysisPipelineError, Exception):
        messages.error(request, "AI 분석에 실패했습니다. 다시 시도하거나 직접 작업을 추가해주세요.")
        return redirect('exams:material_detail', material_id=material.id)

    messages.success(request, "AI 분석이 완료되었습니다.")
    return redirect('exams:task_review', exam_id=material.exam_id)


# =====================================================================
# AI 분석 재시도 (exams:material_retry_analyze) - E-AI-03
# =====================================================================
@login_required
@require_http_methods(["POST"])
def material_retry_analyze(request, material_id):
    material = get_object_or_404(
        StudyMaterial, id=material_id, exam__exam_period__user=request.user
    )

    try:
        retry_analysis(material)
    except DuplicateAnalysisRequestError:
        messages.info(request, "이미 분석 중인 자료입니다.")
        return redirect('exams:material_detail', material_id=material.id)
    except AnalysisNotSupportedError as e:
        messages.error(request, str(e))
        return redirect('exams:material_detail', material_id=material.id)
    except RetryLimitExceededError as e:
        messages.error(request, str(e))
        return redirect('exams:material_detail', material_id=material.id)
    except (AIAnalysisError, AnalysisPipelineError, Exception):
        messages.error(request, "재시도한 AI 분석도 실패했습니다.")
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

    extraction_status = material.status
    extraction_error = material.error_message
    analysis_status = material.analysis_status
    analysis_error = material.analysis_error_message

    # 1. 전체 stage 판정 로직
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

    # ⑥ 아무것도 안 한 PENDING 상태 (PENDING이 튜플에서 빠져서 여기로 옴)
    else:
        stage = "PENDING"

    # 2. 재시도 정보 계산 (API 계약 필수 필드)
    retry_count = material.analysis_retry_count
    retry_remaining = max(0, MAX_RETRY_COUNT - retry_count)

    # 3. 약속된 JSON 응답 스펙 반환
    return JsonResponse({
        "stage": stage,
        "extraction_status": extraction_status,
        "extraction_error_message": extraction_error,
        "analysis_status": analysis_status,
        "analysis_error_message": analysis_error,
        "failed_stage": failed_stage,
        "retry_count": retry_count,
        "retry_remaining": retry_remaining,
        "study_material_id": material.id,
        "exam_id": material.exam_id,
    })


# =====================================================================
# AI 작업 검토 (exams:task_review) 
# =====================================================================
@login_required
@require_http_methods(["GET", "POST"])
def task_review(request, exam_id):
    exam = get_object_or_404(Exam, id=exam_id, exam_period__user=request.user)
    queryset = StudyTask.objects.filter(exam=exam)

    if request.method == 'POST':
        action = request.POST.get('action')
        formset = StudyTaskFormSet(request.POST, queryset=queryset)

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

            if action in ('confirm', 'confirm_and_next'):
                exam.study_tasks.filter(is_confirmed=False).update(is_confirmed=True)
                return redirect('planner:feasibility', period_id=exam.exam_period_id)

            return redirect('exams:task_review', exam_id=exam.id)
    else:
        formset = StudyTaskFormSet(queryset=queryset)

    return render(request, 'exams/task_review.html', {
        'formset': formset,
        'exam': exam,
    })


# =====================================================================
# 학습 작업 직접 추가 (exams:task_create) 
# =====================================================================
@login_required
@require_http_methods(["GET", "POST"])
def study_task_create(request, exam_id):
    exam = get_object_or_404(Exam, id=exam_id, exam_period__user=request.user)

    if request.method == 'POST':
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
            return redirect('exams:task_review', exam_id=exam.id)
    else:
        form = StudyTaskForm()

    return render(request, 'exams/task_form.html', {'form': form, 'exam': exam})  


# =====================================================================
# 학습 작업 확정 (exams:task_confirm) 
# =====================================================================
@login_required
@require_http_methods(["POST"])
def study_task_confirm(request, exam_id):
    exam = get_object_or_404(Exam, id=exam_id, exam_period__user=request.user)
    tasks = StudyTask.objects.filter(exam=exam, is_confirmed=False)

    if not tasks.exists():
        messages.warning(request, "확정할 학습 작업이 없습니다. 먼저 작업을 검토해주세요.")
        return redirect('exams:task_review', exam_id=exam.id)

    for task in tasks:
        task.is_confirmed = True
        task.save()  

    messages.success(request, f"{tasks.count()}개 학습 작업이 확정되었습니다.")
    return redirect('planner:feasibility', period_id=exam.exam_period.id)