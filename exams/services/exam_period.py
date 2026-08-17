# exams/services/exam_period.py
"""
만료된 ACTIVE ExamPeriod를 COMPLETED로 전환하는 Lazy Check 공용 서비스.

period_list/period_create/dashboard/today/calendar처럼 여러 화면이 동일한
판정 로직을 각자 호출해야 하는데, 어느 화면을 거쳤는지에 따라 같은
시험기간의 표시 상태(status)가 달라지는 불일치를 막기 위해 exams/views.py의
private 함수였던 로직을 여기로 분리했다.

락 규칙은 period_complete()의 수동 종료와 동일하다: ExamPeriod
select_for_update() 락 안에서 '판정 → 저장'을 원자적으로 수행해야 한다.
이걸 지키지 않으면
    1) Lazy Check가 PROCESSING 없음을 확인
    2) AI 요청이 ExamPeriod 락을 잡고 material을 PROCESSING으로 선점
    3) Lazy Check가 뒤늦게 COMPLETED로 저장
하는 경쟁이 가능해져서, "AI 호출 완료 후 종료된 시험기간에 StudyTask 저장"과
동일한 버그가 재발한다.
"""
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.choices import ExamPeriodStatus, MaterialStatus
from ..models import ExamPeriod, StudyMaterial


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


def complete_expired_period(period):
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


def complete_expired_periods_for_user(user):
    """사용자의 만료된 ACTIVE ExamPeriod 전체를 대상으로 Lazy Check를 수행한다.

    queryset.exclude(...).update(...) 같은 일괄 UPDATE는 '어떤 시험기간이
    PROCESSING 중인지 확인하는 조회'와 'COMPLETED로 갱신하는 UPDATE' 사이에
    락이 없어 위와 동일한 경쟁이 가능하다. 이를 막기 위해 만료 후보 id만
    뽑은 뒤, 각 시험기간을 개별 트랜잭션에서 select_for_update()로 잠그고
    판정한다. 시험기간 수가 많지 않은 도메인이라 건별 락의 비용은 무시할
    만하다.
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