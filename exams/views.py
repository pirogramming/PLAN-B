import datetime
import uuid
from functools import wraps
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods

from core.choices import ExamPeriodStatus, MaterialStatus, MaterialType
from core.exceptions import AIAnalysisError
from planner.models import DailyPlan, RecoveryPlan
from planner.services.time_estimator import estimate_task_minutes

from .forms import (
    AvailableTimeFormSet,
    ExamForm,
    ExamPeriodForm,
    StudyMaterialForm,
    StudyTaskForm,
    StudyTaskFormSet,
)
from .models import AvailableTime, Exam, ExamPeriod, StudyMaterial, StudyTask
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
    AnalysisPipelineError,
    RetryLimitExceededError,
    StaleAnalysisRunError,
)

logger = logging.getLogger(__name__)


# =====================================================================
# 다중 업로드 정책 상수 (MVP 기준)
# 파일별 StudyMaterial이 독립적이라 일부 실패해도 전체 롤백하지 않고
# 성공한 파일만 저장하는 정책과 함께 사용된다. 값은 OCR/AI 처리 부하를
# 고려해 보수적으로 잡았고, 필요시 조정한다.
# =====================================================================
MAX_BULK_FILES = 5
MAX_SINGLE_FILE_SIZE = 20 * 1024 * 1024   # 20MB (기존 단건 업로드 제한과 동일)
MAX_BULK_TOTAL_SIZE = 50 * 1024 * 1024    # 50MB
MAX_BULK_PROCESS_MATERIALS = 5


# =====================================================================
# 헬퍼 함수: 소유권 검증 + Lazy Check 자동 종료
# =====================================================================
def _has_processing_material(period):
    """해당 시험기간에 PDF 추출(status) 또는 AI 분석(analysis_status)이
    PROCESSING 중인 StudyMaterial이 하나라도 있는지 확인한다.

    #132의 AI/OCR 경로는 'ExamPeriod lock → PROCESSING 선점 → lock 해제 →
    외부 OCR/AI 호출 → 결과 저장' 구조라, lock을 해제한 뒤 실제 호출이
    끝나기 전까지는 ExamPeriod row lock만으로 진행 중 여부를 알 수 없다.
    그 사이 시험기간을 COMPLETED로 만들어버리면(수동 종료든 lazy check든)
    "AI 호출 완료 후 종료된 시험기간에 StudyTask 저장"이 가능해지므로,
    종료/자동종료 판정 전에 이 함수로 반드시 확인해야 한다.
    """
    return StudyMaterial.objects.filter(
        exam__exam_period=period,
    ).filter(
        Q(status=MaterialStatus.PROCESSING) | Q(analysis_status=MaterialStatus.PROCESSING)
    ).exists()


# =====================================================================
# 헬퍼 함수: 만료된 ACTIVE 시험기간 → COMPLETED 전환 (Lazy Check)
# period_complete()의 수동 종료와 동일하게 ExamPeriod select_for_update() 락
# 안에서 '판정 → 저장'을 원자적으로 수행한다. 이걸 지키지 않으면
#   1) Lazy Check가 PROCESSING 없음을 확인
#   2) AI 요청이 ExamPeriod 락을 잡고 material을 PROCESSING으로 선점
#   3) Lazy Check가 뒤늦게 COMPLETED로 저장
# 하는 경쟁이 가능해져서, "AI 호출 완료 후 종료된 시험기간에 StudyTask 저장"과
# 동일한 버그가 재발한다.
# =====================================================================
def _lazy_complete_expired_period_locked(period):
    """단일 ExamPeriod에 대해 select_for_update() 락 안에서 만료+미처리 여부를
    다시 확인하고 필요시 COMPLETED로 전환한 뒤, 잠금이 걸렸던 시점 기준 최신
    인스턴스를 반환한다.

    호출부에서 이미 얕게 조회해 둔 period가 만료 대상으로 '보이지 않으면'
    (ACTIVE가 아니거나 아직 end_date 이전) 락을 아예 열지 않고 그대로
    반환한다 - 대부분의 조회가 여기 해당하므로, 매 요청마다 트랜잭션을 여는
    비용을 피하기 위한 최적화다. 이 사전 체크는 최적화일 뿐 최종 판정이
    아니며, 만료 대상으로 보이는 경우엔 반드시 락 안에서 다시 판정한다.
    """
    if not (
        period.status == ExamPeriodStatus.ACTIVE
        and period.end_date < timezone.localdate()
    ):
        return period

    with transaction.atomic():
        locked = ExamPeriod.objects.select_for_update().get(id=period.id)
        if (
            locked.status == ExamPeriodStatus.ACTIVE
            and locked.end_date < timezone.localdate()
            and not _has_processing_material(locked)
        ):
            locked.status = ExamPeriodStatus.COMPLETED
            locked.save(update_fields=['status'])
        return locked


def _lazy_complete_expired_periods_for_user(user):
    """사용자의 만료된 ACTIVE ExamPeriod 전체를 대상으로 Lazy Check를 수행한다.

    period_list/period_create가 이전에 쓰던 queryset.exclude(...).update(...)
    일괄 처리는 '어떤 시험기간이 PROCESSING 중인지 확인하는 조회'와
    'COMPLETED로 갱신하는 UPDATE' 사이에 락이 없어 위와 동일한 경쟁이
    가능했다. 이를 막기 위해 만료 후보 id만 뽑은 뒤, 각 시험기간을 개별
    트랜잭션에서 select_for_update()로 잠그고 판정한다. 시험기간 수가 많지
    않은 도메인이라 건별 락의 비용은 무시할 만하다.
    """
    expired_period_ids = ExamPeriod.objects.filter(
        user=user,
        status=ExamPeriodStatus.ACTIVE,
        end_date__lt=timezone.localdate(),
    ).values_list('id', flat=True)

    for period_id in expired_period_ids:
        with transaction.atomic():
            period = ExamPeriod.objects.select_for_update().get(id=period_id)
            if (
                period.status == ExamPeriodStatus.ACTIVE
                and period.end_date < timezone.localdate()
                and not _has_processing_material(period)
            ):
                period.status = ExamPeriodStatus.COMPLETED
                period.save(update_fields=['status'])


def _get_owned_exam_period(user, period_id):
    """사용자의 시험기간을 조회하고, 만료된 ACTIVE 상태면 Lazy Check로 즉시
    COMPLETED 전환을 시도한다(전환 여부 판정 자체는
    _lazy_complete_expired_period_locked 참고)."""
    period = get_object_or_404(ExamPeriod, id=period_id, user=user)
    return _lazy_complete_expired_period_locked(period)



# =====================================================================
# 시험기간 잠금 데코레이터 (동시성 방어 + POST만 차단)
# =====================================================================
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
                if period.status in (ExamPeriodStatus.COMPLETED, ExamPeriodStatus.ARCHIVED):
                    messages.error(request, "종료된 시험기간은 수정할 수 없습니다.")
                    return redirect('exams:period_detail', period_id=period.id)
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
                if period.status in (ExamPeriodStatus.COMPLETED, ExamPeriodStatus.ARCHIVED):
                    messages.error(request, "종료된 시험기간의 과목은 수정할 수 없습니다.")
                    return redirect('exams:period_detail', period_id=period.id)
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
                # 1. exam_period_id만 얻기 위한 조회. 이 시점의 status/analysis_status는
                #    아래 판정에 절대 사용하지 않는다 - ExamPeriod 락을 기다리는 동안
                #    다른 요청(AI 분석 등)이 먼저 락을 잡고 이 material을 PROCESSING으로
                #    선점할 수 있기 때문이다.
                material_ref = get_object_or_404(
                    StudyMaterial.objects.select_related('exam__exam_period'),
                    id=material_id,
                    exam__exam_period__user=request.user
                )
                period = ExamPeriod.objects.select_for_update().get(
                    id=material_ref.exam.exam_period_id
                )

                if period.status in (ExamPeriodStatus.COMPLETED, ExamPeriodStatus.ARCHIVED):
                    messages.error(request, "종료된 시험기간의 학습자료는 수정할 수 없습니다.")
                    return redirect('exams:period_detail', period_id=period.id)

                if DailyPlan.objects.filter(exam_period=period).exists():
                    messages.error(request, "이미 계획이 생성된 시험기간의 학습자료는 수정하거나 삭제할 수 없습니다.")
                    return redirect('exams:period_detail', period_id=period.id)

                # 2. ExamPeriod 락을 획득한 *이후*에 material을 다시 조회해서
                #    최신 status/analysis_status로 판정한다. 위 1번에서 락 대기가
                #    있었다면, 그 사이 다른 요청이 먼저 이 ExamPeriod를 잠그고
                #    material을 PROCESSING으로 바꿔놓았을 수 있는데, 이 재조회로
                #    그 변경 사항을 놓치지 않고 반영한다.
                material = get_object_or_404(
                    StudyMaterial,
                    id=material_id,
                    exam__exam_period__user=request.user,
                )

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
    """
    def decorator(view_func):
        @wraps(view_func)
        def wrapped_view(request, material_id, *args, **kwargs):
            if request.method == 'POST':
                with transaction.atomic():
                    # 1. exam_period_id만 얻기 위한 조회. 이 시점의 material은
                    #    claim_func 판정에 절대 쓰지 않는다 - ExamPeriod 락을
                    #    기다리는 동안 다른 요청(삭제 등)이 먼저 락을 잡고
                    #    이 material을 지우거나 상태를 바꿀 수 있기 때문이다.
                    material_ref = get_object_or_404(
                        StudyMaterial.objects.select_related('exam__exam_period'),
                        id=material_id,
                        exam__exam_period__user=request.user
                    )
                    period = ExamPeriod.objects.select_for_update().get(
                        id=material_ref.exam.exam_period_id
                    )

                    if period.status in (ExamPeriodStatus.COMPLETED, ExamPeriodStatus.ARCHIVED):
                        messages.error(request, "종료된 시험기간의 학습자료는 수정할 수 없습니다.")
                        return redirect('exams:period_detail', period_id=period.id)

                    if DailyPlan.objects.filter(exam_period=period).exists():
                        messages.error(request, "이미 계획이 생성된 시험기간의 학습자료는 수정하거나 삭제할 수 없습니다.")
                        return redirect('exams:period_detail', period_id=period.id)

                    # 2. ExamPeriod 락 획득 이후 material을 다시 조회한다.
                    #    락 대기 중 다른 요청이 먼저 락을 잡고 material을 삭제했거나
                    #    상태를 바꿨을 수 있으므로, claim_func에는 이 재조회 결과만
                    #    넘긴다. 삭제된 경우 material_detail로 안내하고 종료한다
                    #    (500 대신 정상적인 사용자 메시지).
                    try:
                        material = StudyMaterial.objects.get(
                            id=material_id,
                            exam__exam_period__user=request.user,
                        )
                    except StudyMaterial.DoesNotExist:
                        messages.error(request, "이미 삭제된 학습자료입니다.")
                        return redirect('exams:period_detail', period_id=period.id)

                    if claim_func is not None:
                        claimed, message, level, extra = claim_func(material)
                        if not claimed:
                            getattr(messages, level)(request, message)
                            return redirect('exams:material_detail', material_id=material.id)
                        kwargs['claim_extra'] = extra
                # atomic 블록 종료 → ExamPeriod 락 해제
            return view_func(request, material_id, *args, **kwargs)
        return wrapped_view
    return decorator


# =====================================================================
def _claim_and_run_for_material(user, exam_id, material_id, claim_func, run_func):
    """material_id 하나에 대해 lock → claim까지 마친 뒤 run_func(material, extra)를
    락 밖에서 실행한다. run_func은 (ok, message) 튜플을 반환해야 한다.
    claim 단계에서 이미 실패하면 run_func은 호출되지 않고 (False, message)를 반환한다.

    exam_id를 조건에 포함해, 로그인 사용자가 소유한 자료라도 요청 URL의
    exam(과목) 소속이 아니면 처리하지 않는다 - 그렇지 않으면 다른 과목의
    material_id를 섞어 보내는 것만으로 그 자료까지 실제 추출/분석이 되어버린다.
    """
    with transaction.atomic():
        try:
            material_ref = StudyMaterial.objects.select_related('exam__exam_period').get(
                id=material_id,
                exam_id=exam_id,
                exam__exam_period__user=user,
            )
        except StudyMaterial.DoesNotExist:
            return False, "존재하지 않는 학습자료입니다."

        period = ExamPeriod.objects.select_for_update().get(
            id=material_ref.exam.exam_period_id
        )

        if period.status in (ExamPeriodStatus.COMPLETED, ExamPeriodStatus.ARCHIVED):
            return False, "종료된 시험기간의 학습자료입니다."

        if DailyPlan.objects.filter(exam_period=period).exists():
            return False, "이미 계획이 생성된 시험기간의 학습자료입니다."

        try:
            material = StudyMaterial.objects.get(
                id=material_id,
                exam_id=exam_id,
                exam__exam_period__user=user,
            )
        except StudyMaterial.DoesNotExist:
            return False, "이미 삭제된 학습자료입니다."

        claimed, message, level, extra = claim_func(material)
        if not claimed:
            return False, message
    # atomic 블록 종료 → ExamPeriod 락 해제, 이후 실제 실행(외부 호출)은 락 밖에서

    return run_func(material, extra)


def _normalize_material_ids(raw_ids):
    """request.POST.getlist("material_ids")의 문자열 값들을 정수 PK로 정규화한다.
    잘못된 값(abc 등)은 PK 조회에 바로 쓰이면 500으로 이어질 수 있어 걸러내고,
    같은 ID가 여러 번 전달돼도 한 번만 처리되도록 최초 등장 순서를 유지한 채
    dedupe한다. (valid_ids, invalid_raw_values) 튜플을 반환한다.
    """
    seen = set()
    valid_ids = []
    invalid = []
    for raw in raw_ids:
        try:
            mid = int(raw)
        except (TypeError, ValueError):
            invalid.append(raw)
            continue
        if mid not in seen:
            seen.add(mid)
            valid_ids.append(mid)
    return valid_ids, invalid

# =====================================================================
# 시험기간 목록 (exams:period_list) - 일괄 Lazy Check 자동 종료 적용
# =====================================================================
@login_required
@require_http_methods(["GET"])
def period_list(request):
    # end_date가 지난 ACTIVE 시험기간을 개별 락 기준으로 일괄 COMPLETED 처리
    _lazy_complete_expired_periods_for_user(request.user)

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
    # period_list/period_detail을 거치지 않고 바로 생성 화면으로 들어오는 경우에도
    # 동일한 Lazy Check를 먼저 수행한다.
    _lazy_complete_expired_periods_for_user(request.user)

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

            # FE1(신예원): 시험기간 생성 직후에는 허브(시험기간 홈)로 보내지 않고
            # 피그마 3→4페이지 순서 그대로 '과목 등록'으로 바로 이어지게 한다.
            return redirect('exams:subject_create', period_id=period.id)
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

                AvailableTime.objects.filter(exam_period=updated).exclude(
                    date__range=(updated.start_date, updated.end_date)
                ).delete()

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
    period_title = period.title

    with transaction.atomic():
        DailyPlan.objects.filter(exam_period=period).delete()
        RecoveryPlan.objects.filter(exam_period=period).delete()
        period.delete()

    messages.success(request, f"'{period_title}' 시험기간과 관련 학습 계획이 모두 삭제되었습니다.")
    return redirect('exams:period_list')


# =====================================================================
# 시험기간 수동 종료 (exams:period_complete)
# =====================================================================
@login_required
@require_http_methods(["POST"])
def period_complete(request, period_id):
    """
    시험기간 수동 종료. #132의 AI/OCR 경로('ExamPeriod lock → PROCESSING 선점 →
    lock 해제 → 외부 호출 → 결과 저장')와 동일한 기준으로 직렬화하기 위해,
    ExamPeriod를 select_for_update()로 잠근 뒤 PROCESSING 중인 학습자료가 있으면
    종료를 거부한다. 이렇게 하면 AI/OCR 쪽과 이 뷰 중 어느 쪽이 먼저 락을
    잡든 같은 규칙으로 순서가 정해진다.
    """
    with transaction.atomic():
        period = get_object_or_404(
            ExamPeriod.objects.select_for_update(),
            id=period_id,
            user=request.user,
        )

        if period.status != ExamPeriodStatus.ACTIVE:
            messages.info(request, "이미 완료되거나 보관 처리된 시험기간입니다.")
            return redirect('exams:period_list')

        if _has_processing_material(period):
            messages.error(request, "PDF 추출 또는 AI 분석이 진행 중인 학습자료가 있어 시험기간을 종료할 수 없습니다. 처리가 끝난 후 다시 시도해주세요.")
            return redirect('exams:period_list')

        period.status = ExamPeriodStatus.COMPLETED
        period.save(update_fields=['status'])
        period_title = period.title
    # atomic 블록 종료 → ExamPeriod 락 해제

    messages.success(request, f"'{period_title}' 시험기간이 완료 처리되었습니다.")
    return redirect('exams:period_list')


# =====================================================================
# 시험기간 상세 (exams:period_detail) - Lazy Check 헬퍼 호출 연결
# =====================================================================
@login_required
@require_http_methods(["GET"])
def period_detail(request, period_id):
    period = _get_owned_exam_period(request.user, period_id)
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
    """
    계획 생성 후 전용 가용시간 수정 화면. plan_generate()와의 직렬화, 그리고
    "종료된 시험기간은 읽기 전용" 정책을 함께 지키기 위해 available_time_update와
    동일하게 POST 처리 전체를 ExamPeriod row lock(select_for_update) 안에서
    수행하고, DailyPlan/상태 확인부터 저장까지 같은 트랜잭션에서 끝낸다.
    """
    if request.method == 'POST':
        with transaction.atomic():
            period = get_object_or_404(
                ExamPeriod.objects.select_for_update(),
                id=period_id,
                user=request.user,
            )

            if not DailyPlan.objects.filter(exam_period=period).exists():
                return redirect('exams:period_detail', period_id=period.id)

            if period.status in (ExamPeriodStatus.COMPLETED, ExamPeriodStatus.ARCHIVED):
                messages.error(request, "종료된 시험기간은 가용 시간을 수정할 수 없습니다.")
                return redirect('exams:period_manage', period_id=period.id)

            queryset = AvailableTime.objects.filter(exam_period=period).order_by('date')
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
        # atomic 블록 종료 → ExamPeriod 락 해제
        return render(request, 'exams/period_manage_available_time.html', {
            'formset': formset,
            'period': period,
        })

    # ================================================================
    # GET
    # ================================================================
    period = get_object_or_404(ExamPeriod, id=period_id, user=request.user)
    if not DailyPlan.objects.filter(exam_period=period).exists():
        return redirect('exams:period_detail', period_id=period.id)
    queryset = AvailableTime.objects.filter(exam_period=period).order_by('date')
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
            return redirect('exams:subject_create', period_id=period.id)
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
    period = get_object_or_404(ExamPeriod, id=period_id, user=request.user)
    exam = get_object_or_404(Exam, id=exam_id, exam_period=period)

    if request.method == "POST":
        form = ExamForm(request.POST, instance=exam, exam_period=period)
        if form.is_valid():
            form.save()
            return redirect("exams:period_detail", period_id=period.id)
    else:
        form = ExamForm(instance=exam, exam_period=period)

    return render(request, "exams/subject_form.html", {'form': form, 'period': period, 'exam': exam})


# =====================================================================
# 과목 삭제 (exams:subject_delete) 
# =====================================================================
@login_required
@check_exam_period_locked_by_period_id
@require_http_methods(["POST"])
def subject_delete(request, period_id, exam_id):
    period = get_object_or_404(ExamPeriod, id=period_id, user=request.user)
    exam = get_object_or_404(Exam, id=exam_id, exam_period=period)

    subject_name = exam.subject_name
    exam.delete()

    messages.success(request, f"'{subject_name}' 과목이 삭제되었습니다.")
    return redirect("exams:period_detail", period_id=period.id)


# =====================================================================
# 가능시간 입력 (exams:available_time_update)
# =====================================================================
@login_required
@require_http_methods(["GET", "POST"])
def available_time_update(request, period_id):
    """
    가용시간 수정. 계획(DailyPlan) 존재 여부와 무관하게 항상 허용하되,
    과거/마감된 날짜 차단은 save_available_time_formset()이 담당한다.

    예전에는 @check_exam_period_locked_by_period_id를 적용해서 DailyPlan이
    하나라도 있으면 POST 자체를 막았는데, 이는 "계획 생성 후에도 가용시간
    수정은 허용한다"는 정책과 충돌해서 제거했다 (관련 논의: PR 리뷰).

    plan_generate()와의 직렬화는 여전히 필요하므로, DailyPlan 존재 여부로
    차단하는 대신 ExamPeriod row lock만 POST 처리 전체에 건다. 이렇게 하면
    plan_generate()가 같은 ExamPeriod를 잠그고 finalized_at/가용시간을 읽는
    동안 이 뷰가 끼어들어 값을 바꾸는 것만 막힌다.
    """
    if request.method == "POST":
        with transaction.atomic():
            period = get_object_or_404(
                ExamPeriod.objects.select_for_update(),
                id=period_id,
                user=request.user,
            )

            if period.status in (ExamPeriodStatus.COMPLETED, ExamPeriodStatus.ARCHIVED):
                messages.error(request, "종료된 시험기간은 가용 시간을 수정할 수 없습니다.")
                return redirect('exams:period_detail', period_id=period.id)

            queryset = AvailableTime.objects.filter(
                exam_period=period
            ).order_by("date")

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
        # atomic 블록 종료 → ExamPeriod 락 해제

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
    period = get_object_or_404(ExamPeriod, id=period_id, user=request.user)
    queryset = AvailableTime.objects.filter(exam_period=period).order_by("date")
    formset = AvailableTimeFormSet(queryset=queryset)
    next_url = request.GET.get("next") or request.META.get("HTTP_REFERER", "")

    return render(
        request,
        "exams/available_time_form.html",
        {"formset": formset, "period": period, "next": next_url},
    )


# =====================================================================
# 자료 등록 (exams:material_create)
# 다중 업로드 지원: <input type="file" name="files" multiple>로 여러 PDF를
# 한 번에 받는다. 파일별 StudyMaterial이 서로 독립적이므로, 일부 파일이
# 검증에 실패해도 전체를 롤백하지 않고 성공한 파일만 저장한다 (팀 논의로
# 결정: 하나 실패했다고 정상 파일까지 재업로드시킬 이유가 없음). 실패한
# 파일은 파일명+사유를 메시지로 안내한다.
# 텍스트(TEXT) 자료는 파일이 없는 별도 흐름이라 다중 업로드 대상에서 제외하고
# 기존처럼 단일 폼으로만 등록한다.
# =====================================================================
@login_required
@check_exam_period_locked_by_exam_id
@require_http_methods(["GET", "POST"])
def material_create(request, exam_id):
    exam = get_object_or_404(Exam, id=exam_id, exam_period__user=request.user)

    if request.method == "POST":
        material_type = request.POST.get("material_type", MaterialType.PDF)

        # TEXT 자료는 파일이 없는 기존 단일 흐름을 그대로 유지한다.
        if material_type == MaterialType.TEXT:
            form = StudyMaterialForm(request.POST, request.FILES)
            if form.is_valid():
                material = form.save(commit=False)
                material.exam = exam
                material.status = MaterialStatus.COMPLETED
                material.save()
                return redirect("exams:material_detail", material_id=material.id)
            return render(request, "exams/material_form.html", {"form": form, "exam": exam})

        # material_type이 TEXT가 아니면 전부 PDF 흐름으로 취급했는데, 폼 검증에서
        # 다시 PDF 여부를 걸러내긴 하지만 여기서도 명시적으로 막아 의도치 않은
        # material_type 값(오타, 조작된 요청 등)이 조용히 PDF 흐름을 타는 걸 방지한다.
        if material_type != MaterialType.PDF:
            messages.error(request, "지원하지 않는 자료 유형입니다.")
            return render(request, "exams/material_form.html", {"exam": exam, "form": StudyMaterialForm()})

        # PDF 다중 업로드 흐름
        files = request.FILES.getlist("files") or request.FILES.getlist("file")
        if not files:
            messages.error(request, "업로드할 파일을 선택해주세요.")
            return render(request, "exams/material_form.html", {"exam": exam, "form": StudyMaterialForm()})

        if len(files) > MAX_BULK_FILES:
            messages.error(request, f"한 번에 최대 {MAX_BULK_FILES}개까지 업로드할 수 있습니다.")
            return render(request, "exams/material_form.html", {"exam": exam, "form": StudyMaterialForm()})

        total_size = sum(f.size for f in files)
        if total_size > MAX_BULK_TOTAL_SIZE:
            messages.error(request, "전체 업로드 용량이 50MB를 초과했습니다.")
            return render(request, "exams/material_form.html", {"exam": exam, "form": StudyMaterialForm()})

        created, errors = [], []
        for f in files:
            if f.size > MAX_SINGLE_FILE_SIZE:
                errors.append(f"{f.name}: 파일 용량이 20MB를 초과했습니다.")
                continue

            # 다중 업로드는 파일별 title 입력 UI가 없으므로, 파일명에서
            # 확장자를 뗀 값을 title로 자동 채운다.
            auto_title = f.name.rsplit(".", 1)[0] if "." in f.name else f.name

            form = StudyMaterialForm(
                data={"material_type": MaterialType.PDF, "title": auto_title},
                files={"file": f},
            )
            if form.is_valid():
                material = form.save(commit=False)
                material.exam = exam
                # 파일별로 개별 저장한다 - 전체를 하나의 트랜잭션으로 묶지 않는 이유는
                # 뒤 파일이 실패해도 앞서 저장에 성공한 파일이 함께 롤백되면 안 되기 때문.
                material.save()
                created.append(material)
            else:
                errors.append(f"{f.name}: {form.errors.as_text()}")

        if created:
            messages.success(request, f"{len(created)}개 자료가 업로드되었습니다.")
        for e in errors:
            messages.error(request, e)

        return redirect("exams:period_detail", period_id=exam.exam_period_id)
    else:
        form = StudyMaterialForm()

    return render(request, "exams/material_form.html", {"form": form, "exam": exam})


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
# 실제 추출 로직은 _extract_one()으로 분리했다 - 단건 뷰(material_extract)와
# 일괄 뷰(material_bulk_extract)가 claim 이후 처리를 완전히 동일한 함수로
# 수행하게 하기 위함이며, 로직 자체는 기존과 동일하고 위치만 이동했다.
# =====================================================================
class StaleExtractionRunError(Exception):
    """이 실행(extraction_run_id)이 결과를 저장하기 전에 이미 다른(더 최신) 실행이 이어받음."""
    pass


STALE_EXTRACTION_TIMEOUT_SECONDS = 300


def _claim_material_for_extraction(material):
    if material.material_type != MaterialType.PDF:
        return False, "PDF 자료만 텍스트 추출이 가능합니다.", "error", None
    if not material.file:
        return False, "첨부된 PDF 파일이 없습니다.", "error", None

    now = timezone.now()
    stale_cutoff = now - timezone.timedelta(seconds=STALE_EXTRACTION_TIMEOUT_SECONDS)
    run_id = uuid.uuid4()

    updated = StudyMaterial.objects.filter(pk=material.pk).exclude(
        analysis_status__in=[MaterialStatus.PROCESSING, MaterialStatus.COMPLETED]
    ).filter(
        Q(status__in=[MaterialStatus.PENDING, MaterialStatus.FAILED, MaterialStatus.COMPLETED])
        | Q(status=MaterialStatus.PROCESSING, extraction_started_at__lt=stale_cutoff)
        | Q(status=MaterialStatus.PROCESSING, extraction_started_at__isnull=True)
    ).update(
        status=MaterialStatus.PROCESSING,
        error_message=None,
        extraction_started_at=now,
        extraction_run_id=run_id,
    )

    if not updated:
        material.refresh_from_db(fields=['status', 'analysis_status'])
        if material.status == MaterialStatus.PROCESSING:
            return False, "이미 PDF 텍스트를 추출 중인 자료입니다.", "info", None
        if material.analysis_status == MaterialStatus.PROCESSING:
            return False, "AI 분석이 진행 중인 자료는 다시 추출할 수 없습니다.", "error", None
        return False, "이미 AI 분석이 완료된 자료입니다. 다시 추출하려면 먼저 작업 검토 화면에서 확인해주세요.", "error", None

    return True, None, None, run_id


def _extract_one(material, run_id):
    """claim(run_id 선점) 이후 실제 PDF 텍스트 추출을 수행한다.
    (ok, message, level) 튜플을 반환하며, level은 messages.<level>() 호출에
    그대로 쓸 수 있다. 저장은 extraction_run_id가 이 실행의 run_id와 일치할
    때만 적용된다(_save_if_owner) - stale timeout으로 다른 실행이 이미
    이 material을 재선점했다면, 이 실행의 결과로 그걸 덮어쓰지 않기 위함이다.
    material_extract, material_bulk_extract가 공통으로 호출한다.
    """
    material_id = material.pk
    previous_extracted_text = material.extracted_text

    def _save_if_owner(**fields):
        updated = StudyMaterial.objects.filter(
            pk=material.pk, extraction_run_id=run_id,
        ).update(**fields)
        if not updated:
            raise StaleExtractionRunError(
                f"extraction_run_id 불일치로 저장 무시 (material_id={material_id})"
            )

    try:
        extracted = extract_text_from_pdf(material.file)
    except PdfExtractionError as e:
        try:
            _save_if_owner(status=MaterialStatus.FAILED, error_message=str(e))
        except StaleExtractionRunError:
            logger.info(f"추출 실행이 완료 직전 다른 실행에 선점됨 (material_id={material_id})")
            return False, "다른 요청이 먼저 이 자료를 처리했습니다. 최신 상태를 다시 확인해주세요.", "info"
        return False, "PDF 텍스트 추출에 실패했습니다.", "error"
    except Exception:
        logger.exception(f"PDF 추출 중 예기치 못한 시스템 오류 발생 (material_id={material_id})")
        try:
            _save_if_owner(status=MaterialStatus.FAILED, error_message="알 수 없는 오류로 추출에 실패했습니다.")
        except StaleExtractionRunError:
            logger.info(f"추출 실행이 완료 직전 다른 실행에 선점됨 (material_id={material_id})")
            return False, "다른 요청이 먼저 이 자료를 처리했습니다. 최신 상태를 다시 확인해주세요.", "info"
        return False, "PDF 추출 처리 중 알 수 없는 시스템 오류가 발생했습니다.", "error"

    if not extracted:
        try:
            _save_if_owner(
                status=MaterialStatus.FAILED,
                error_message="텍스트를 추출할 수 없습니다. 스캔 이미지 PDF는 지원하지 않습니다.",
            )
        except StaleExtractionRunError:
            logger.info(f"추출 실행이 완료 직전 다른 실행에 선점됨 (material_id={material_id})")
            return False, "다른 요청이 먼저 이 자료를 처리했습니다. 최신 상태를 다시 확인해주세요.", "info"
        return False, "텍스트를 추출하지 못했습니다. 스캔 이미지 PDF일 수 있어요.", "warning"

    update_fields = {
        'status': MaterialStatus.COMPLETED,
        'extracted_text': extracted,
        'error_message': None,
    }
    if extracted != previous_extracted_text:
        update_fields.update(
            analysis_status=MaterialStatus.PENDING,
            analysis_error_message=None,
            analysis_retry_count=0,
        )

    try:
        _save_if_owner(**update_fields)
    except StaleExtractionRunError:
        logger.info(f"추출 실행이 완료 직전 다른 실행에 선점됨 (material_id={material_id})")
        return False, "다른 요청이 먼저 이 자료를 처리했습니다. 최신 상태를 다시 확인해주세요.", "info"

    return True, "PDF 텍스트 추출이 완료되었습니다.", "success"


@login_required
@check_exam_period_not_locked_by_material_id(claim_func=_claim_material_for_extraction)
@require_http_methods(["POST"])
def material_extract(request, material_id, claim_extra=None):
    material = get_object_or_404(
        StudyMaterial, id=material_id, exam__exam_period__user=request.user
    )
    # material_type / file / status 선점, run_id 발급은 claim_func가 락 안에서
    # 이미 끝냈으므로 여기서는 바로 추출을 진행한다.
    ok, msg, level = _extract_one(material, claim_extra)
    getattr(messages, level)(request, msg)
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
# AI 분석 실행 (exams:material_analyze)
# 실제 분석 실행 로직은 _run_analysis()로 분리했다 (is_retry로 재시도 문구만
# 분기) - material_analyze, material_retry_analyze, material_bulk_analyze가
# claim 이후 처리를 동일한 함수로 수행한다.
# =====================================================================
def _claim_material_for_analysis(material):
    if material.status != MaterialStatus.COMPLETED:
        return False, "텍스트 추출이 완료된 자료만 AI 분석을 시작할 수 있습니다.", "error", None
    try:
        run_id = claim_analysis_run(material, is_retry=False)
    except DuplicateAnalysisRequestError:
        return False, "이미 분석 중이거나 처리된 자료입니다.", "info", None
    return True, None, None, run_id


def _run_analysis(material, run_id, material_id, *, is_retry=False):
    """claim(run_id 선점) 이후 실제 AI 분석 실행을 수행한다.
    (ok, message, level) 튜플을 반환한다.
    """
    try:
        run_claimed_analysis(material, run_id)
        return True, "AI 분석이 완료되었습니다.", "success"
    except StaleAnalysisRunError:
        if is_retry:
            logger.info(f"AI 재시도 실행이 완료 직전 다른 실행에 선점됨 (material_id={material_id})")
        else:
            logger.info(f"AI 분석 실행이 완료 직전 다른 실행에 선점됨 (material_id={material_id})")
        return False, "다른 요청이 먼저 이 자료를 처리했습니다. 최신 상태를 다시 확인해주세요.", "info"
    except (AIAnalysisError, AnalysisPipelineError):
        if is_retry:
            return False, "재시도한 AI 분석도 실패했습니다.", "error"
        return False, "AI 분석에 실패했습니다. 다시 시도하거나 직접 작업을 추가해주세요.", "error"
    except Exception:
        if is_retry:
            logger.exception(f"AI 분석 재시도 중 예기치 못한 시스템 오류 발생 (material_id={material_id})")
            return False, "AI 분석 재시도 처리 중 알 수 없는 시스템 오류가 발생했습니다.", "error"
        logger.exception(f"AI 분석 실행 중 예기치 못한 시스템 오류 발생 (material_id={material_id})")
        return False, "AI 분석 처리 중 알 수 없는 시스템 오류가 발생했습니다.", "error"


@login_required
@check_exam_period_not_locked_by_material_id(claim_func=_claim_material_for_analysis)
@require_http_methods(["POST"])
def material_analyze(request, material_id, claim_extra=None):
    material = get_object_or_404(
        StudyMaterial, id=material_id, exam__exam_period__user=request.user
    )

    ok, msg, level = _run_analysis(material, claim_extra, material_id, is_retry=False)
    getattr(messages, level)(request, msg)
    if not ok:
        return redirect('exams:material_detail', material_id=material.id)

    return redirect('exams:task_review', exam_id=material.exam_id)



# =====================================================================
# AI 분석 재시도 (exams:material_retry_analyze)
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

    ok, msg, level = _run_analysis(material, claim_extra, material_id, is_retry=True)
    getattr(messages, level)(request, msg)
    if not ok:
        return redirect('exams:material_detail', material_id=material.id)

    return redirect('exams:task_review', exam_id=material.exam_id)

# =====================================================================
# AI 분석 상태 조회 (exams:material_analysis_status)
# =====================================================================
@login_required
@require_http_methods(["GET"])
def material_analysis_status(request, material_id):
    material = get_object_or_404(
        StudyMaterial, id=material_id, exam__exam_period__user=request.user
    )

    analysis_data = get_analysis_status(material)

    extraction_status = material.status
    extraction_error = material.error_message

    extraction_is_stale = False
    if extraction_status == MaterialStatus.PROCESSING:
        if material.extraction_started_at is None:
            extraction_is_stale = True
        else:
            elapsed = (timezone.now() - material.extraction_started_at).total_seconds()
            extraction_is_stale = elapsed >= STALE_EXTRACTION_TIMEOUT_SECONDS

    analysis_status = analysis_data["status"]
    analysis_error = analysis_data["error_message"]
    retry_count = analysis_data["retry_count"]
    retry_remaining = analysis_data["retry_remaining"]
    can_retry = analysis_data["can_retry"]
    retry_after_seconds = analysis_data["retry_after_seconds"]
    is_stale = analysis_data["is_stale"]

    failed_stage = None

    if extraction_status == MaterialStatus.PROCESSING:
        stage = "EXTRACTING"
    elif extraction_status == MaterialStatus.FAILED:
        stage = "FAILED"
        failed_stage = "EXTRACTION"
    elif analysis_status == MaterialStatus.PROCESSING:
        stage = "ANALYZING"
    elif analysis_status == MaterialStatus.FAILED:
        stage = "FAILED"
        failed_stage = "ANALYSIS"
    elif (
        extraction_status == MaterialStatus.COMPLETED
        and analysis_status == MaterialStatus.COMPLETED
    ):
        stage = "COMPLETED"
    else:
        stage = "PENDING"

    return JsonResponse({
        "stage": stage,
        "extraction_status": extraction_status,
        "extraction_error_message": extraction_error,
        "extraction_is_stale": extraction_is_stale,
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
# 자료 일괄 텍스트 추출 (exams:material_bulk_extract)
# 여러 자료를 한 번에 추출한다. 별도의 추출 로직을 새로 만들지 않고 단건과
# 동일한 claim(_claim_material_for_extraction) + 실행(_extract_one)을
# material_ids 개수만큼 반복 호출하며, 각 material_id는
# _claim_and_run_for_material()을 통해 단건 데코레이터와 동일한 잠금 순서로
# 처리된다. 일부 자료가 실패해도(이미 처리 중, 삭제됨 등) 나머지는 계속
# 진행하고, 끝에 성공 개수와 실패 사유를 함께 안내한다.
# =====================================================================
@login_required
@require_http_methods(["POST"])
def material_bulk_extract(request, exam_id):
    exam = get_object_or_404(Exam, id=exam_id, exam_period__user=request.user)
    raw_material_ids = request.POST.getlist("material_ids")

    if not raw_material_ids:
        messages.error(request, "추출할 자료를 선택해주세요.")
        return redirect('exams:period_detail', period_id=exam.exam_period_id)

    material_ids, invalid_ids = _normalize_material_ids(raw_material_ids)
    for raw in invalid_ids:
        messages.error(request, f"잘못된 자료 ID입니다: {raw}")

    if not material_ids:
        messages.error(request, "추출할 자료를 선택해주세요.")
        return redirect('exams:period_detail', period_id=exam.exam_period_id)

    if len(material_ids) > MAX_BULK_PROCESS_MATERIALS:
        messages.error(request, f"한 번에 최대 {MAX_BULK_PROCESS_MATERIALS}개까지 처리할 수 있습니다.")
        return redirect('exams:period_detail', period_id=exam.exam_period_id)

    success_count = 0
    fail_messages = []

    for material_id in material_ids:
        def _run(material, extra):
            ok, msg, _level = _extract_one(material, extra)
            return ok, msg

        ok, msg = _claim_and_run_for_material(
            request.user, exam.id, material_id, _claim_material_for_extraction, _run
        )
        if ok:
            success_count += 1
        else:
            fail_messages.append(f"자료 #{material_id}: {msg}")

    if success_count:
        messages.success(request, f"{success_count}개 자료 추출이 완료되었습니다.")
    for m in fail_messages:
        messages.error(request, m)

    return redirect('exams:period_detail', period_id=exam.exam_period_id)


# =====================================================================
# 자료 일괄 AI 분석 (exams:material_bulk_analyze)
# material_bulk_extract와 동일한 패턴 - 단건 claim(_claim_material_for_analysis)
# + 실행(_run_analysis)을 material_ids 개수만큼 반복 호출한다. 일부 실패해도
# 나머지는 계속 처리한다.
# =====================================================================
@login_required
@require_http_methods(["POST"])
def material_bulk_analyze(request, exam_id):
    exam = get_object_or_404(Exam, id=exam_id, exam_period__user=request.user)
    raw_material_ids = request.POST.getlist("material_ids")

    if not raw_material_ids:
        messages.error(request, "분석할 자료를 선택해주세요.")
        return redirect('exams:period_detail', period_id=exam.exam_period_id)

    material_ids, invalid_ids = _normalize_material_ids(raw_material_ids)
    for raw in invalid_ids:
        messages.error(request, f"잘못된 자료 ID입니다: {raw}")

    if not material_ids:
        messages.error(request, "분석할 자료를 선택해주세요.")
        return redirect('exams:period_detail', period_id=exam.exam_period_id)

    if len(material_ids) > MAX_BULK_PROCESS_MATERIALS:
        messages.error(request, f"한 번에 최대 {MAX_BULK_PROCESS_MATERIALS}개까지 처리할 수 있습니다.")
        return redirect('exams:period_detail', period_id=exam.exam_period_id)

    success_count = 0
    fail_messages = []

    for material_id in material_ids:
        def _run(material, extra):
            ok, msg, _level = _run_analysis(material, extra, material.id, is_retry=False)
            return ok, msg

        ok, msg = _claim_and_run_for_material(
            request.user, exam.id, material_id, _claim_material_for_analysis, _run
        )
        if ok:
            success_count += 1
        else:
            fail_messages.append(f"자료 #{material_id}: {msg}")

    if success_count:
        messages.success(request, f"{success_count}개 자료 AI 분석이 완료되었습니다.")
    for m in fail_messages:
        messages.error(request, m)

    return redirect('exams:task_review', exam_id=exam_id)

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
            if action in ('confirm', 'confirm_and_next'):
                # '모든 과목 저장하고 다음으로' 버튼: 이름 그대로 동작하도록,
                # 지금 보고 있는 과목뿐 아니라 같은 시험기간의 모든 과목의
                # 미확정 학습 작업을 한 번에 확정 처리한 뒤 바로 feasibility로 이동한다.
                StudyTask.objects.filter(
                    exam__exam_period_id=exam.exam_period_id,
                    is_confirmed=False,
                ).update(is_confirmed=True)
                return redirect('planner:feasibility', period_id=exam.exam_period_id)

            return redirect('exams:task_review', exam_id=exam.id)
            
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