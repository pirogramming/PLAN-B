import datetime
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_http_methods
from django.contrib import messages
from django.http import JsonResponse

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
    DuplicateAnalysisRequestError,
    AnalysisNotSupportedError,
    RetryLimitExceededError,
    AnalysisPipelineError,
)
from django.db import transaction


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
    - 생성 성공 시 '시험기간 상세'로 이동 (표의 연동화면 기준)
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
            return redirect('exams:period_detail', period_id=period.id)  # 표: 연동화면=시험기간 상세
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
    """
    period_create/update에서 이미 0분으로 AvailableTime을 생성해두므로
    여기서는 값만 채우는 '수정' 개념 (extra=0 formset으로 충분)
    """
    period = get_object_or_404(ExamPeriod, id=period_id, user=request.user)
    queryset = AvailableTime.objects.filter(exam_period=period).order_by('date')

    if request.method == 'POST':
        formset = AvailableTimeFormSet(request.POST, queryset=queryset)
        if formset.is_valid():
            instances = formset.save(commit=False)
            for instance in instances:
                instance.exam_period = period
                instance.save()
            return redirect('exams:period_detail', period_id=period.id)  # 표: 연동화면=시험기간 상세
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
    """
    D-MAT-03: PDF 텍스트 추출
    - status: PENDING/FAILED → PROCESSING → COMPLETED(+extracted_text) / FAILED(+error_message)
    """
    material = get_object_or_404(
        StudyMaterial, id=material_id, exam__exam_period__user=request.user
    )

    if material.material_type != MaterialType.PDF:
        messages.error(request, "PDF 자료만 텍스트 추출이 가능합니다.")
        return redirect('exams:material_detail', material_id=material.id)

    if not material.file:
        messages.error(request, "첨부된 PDF 파일이 없습니다.")
        return redirect('exams:material_detail', material_id=material.id)

    # 아래 세 조건 중 하나라도 걸리면 재추출을 막는다.
    #   - status(추출 상태)가 이미 PROCESSING (다른 요청이 추출 중)
    #   - analysis_status가 PROCESSING (AI 분석 진행 중 - 어느 텍스트 기준인지 꼬임)
    #   - analysis_status가 COMPLETED (이미 이 텍스트 기준으로 분석 결과가 있음)
    # 조회 후 파이썬에서 검사하는 방식은 동시 요청 사이의 경쟁 상태가 남으므로,
    # 조건부 UPDATE 하나로 "확인 + PROCESSING 전이"를 원자적으로 처리한다
    # (analysis_orchestrator._save_tasks_with_estimates()의 재검증과 짝을 이루는 방어).
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
    # 재추출 성공은 곧 "새로운 분석 대상"이 됐다는 뜻이다. 이전 텍스트를 기준으로
    # 쌓였던 AI 분석 상태(특히 FAILED 사유, 재시도 횟수)는 새 텍스트와 무관하므로
    # 초기화해서, 사용자가 새 텍스트로 최초 분석부터 다시 시작할 수 있게 한다.
    # (추출 실패 케이스에서는 extracted_text 자체가 안 바뀌므로 여기서 건드리지 않는다.)
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
    """
    E-AI-01: AI 분석 요청.
    analysis_status: PENDING -> PROCESSING -> COMPLETED/FAILED

    텍스트 추출(status)이 아직 COMPLETED가 아니면 (PDF 추출 전, 실패 등)
    분석 자체를 시작하지 않는다 - 추출 상태와 분석 상태는 별개 필드지만,
    추출이 안 끝난 자료를 분석할 수는 없기 때문.
    """
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
    except (AIAnalysisError, AnalysisPipelineError):
        # 실패 사유는 이미 material.analysis_error_message에 저장돼 있음
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
    """
    E-AI-03: AI 분석 재시도. FAILED 상태 + 재시도 횟수(2회) 남아있을 때만 허용.
    조건에 안 맞으면 analysis_orchestrator가 던지는 예외를 그대로 사용자 메시지로 변환한다.
    """
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
    except (AIAnalysisError, AnalysisPipelineError):
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
    "분석 중..." 화면에서 주기적으로 호출해 상태 변화를 확인하는 용도.

    프론트가 material_analyze()를 순차 자동 호출하는 방식으로 가면서, 성공/실패
    판단을 이 엔드포인트 하나로만 하기로 확정했다. 텍스트 추출 상태
    (material.status/error_message, BE2 담당 필드)와 AI 분석 상태
    (material.analysis_status/analysis_error_message, 이 파일 담당)를 하나의
    스키마로 합쳐서 내려준다 (BE2와 필드명·구조 합의 완료).

    최종 응답 스키마:
        {
            "stage": "PENDING | EXTRACTING | ANALYZING | COMPLETED | FAILED",
            "extraction_status": "pending | processing | completed | failed",
            "extraction_error_message": str | None,
            "analysis_status": "pending | processing | completed | failed",
            "analysis_error_message": str | None,
            "failed_stage": "EXTRACTION | ANALYSIS" | None,
            "retry_count": int,
            "retry_remaining": int,
        }
    extraction_status/analysis_status는 StudyMaterial 모델 필드 값을 그대로 내려서
    소문자다 (MaterialStatus TextChoices 자체가 소문자). stage/failed_stage는 이
    엔드포인트가 새로 만드는 값이라 대문자로 통일했다.

    stage 우선순위가 "추출 상태 먼저, 분석 상태 나중"인 이유: material_extract()의
    원자적 방어(analysis_status가 PROCESSING/COMPLETED면 재추출 자체가 막힘) 덕분에,
    material.status가 PROCESSING/FAILED로 남아있다는 건 "지금 추출(재추출 포함)
    작업이 진행/실패한 것"이 확정적으로 최신 상황이라는 뜻이다. 그래서 이 경우엔
    analysis_status에 남아있는 이전 분석 기록(예: 재추출 전의 예전 실패 사유)보다
    추출 상태를 우선해서 보여준다 - 순서를 반대로 하면(분석 실패를 먼저 체크하면),
    재추출이 한창 진행 중인데도 stage가 잘못 FAILED로 나오는 문제가 생긴다.

    failed_stage: stage가 "FAILED"일 때, 추출 단계에서 실패한 건지("EXTRACTION")
    분석 단계에서 실패한 건지("ANALYSIS") 구분해서 알려준다. 둘 다 아니면 None.
    """
    material = get_object_or_404(
        StudyMaterial, id=material_id, exam__exam_period__user=request.user
    )
    analysis_data = get_analysis_status(material)  # 기존 서비스 함수, 시그니처 안 바뀜

    if material.status == MaterialStatus.FAILED:
        stage = "FAILED"
        failed_stage = "EXTRACTION"
    elif material.status == MaterialStatus.PROCESSING:
        stage = "EXTRACTING"
        failed_stage = None
    elif analysis_data["status"] == MaterialStatus.PROCESSING:
        stage = "ANALYZING"
        failed_stage = None
    elif analysis_data["status"] == MaterialStatus.FAILED:
        stage = "FAILED"
        failed_stage = "ANALYSIS"
    elif analysis_data["status"] == MaterialStatus.COMPLETED:
        stage = "COMPLETED"
        failed_stage = None
    else:
        stage = "PENDING"
        failed_stage = None

    return JsonResponse({
        "stage": stage,
        "extraction_status": material.status,
        "extraction_error_message": material.error_message,
        "analysis_status": analysis_data["status"],
        "analysis_error_message": analysis_data["error_message"],
        "failed_stage": failed_stage,
        "retry_count": analysis_data["retry_count"],
        "retry_remaining": analysis_data["retry_remaining"],
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
            return redirect('exams:task_review', exam_id=exam.id)
    else:
        formset = StudyTaskFormSet(queryset=queryset)

    return render(request, 'exams/task_review.html', {'formset': formset, 'exam': exam})

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