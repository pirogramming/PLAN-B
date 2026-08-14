"""
가용시간(AvailableTime) 저장 시 공통으로 지켜야 하는 정책을 한 곳에 모은다.
exams:available_time_update(공용 화면)와 exams:period_manage_available_time
(관리 화면)이 같이 쓴다 — 이 파일을 거치지 않고 AvailableTimeFormSet.save()를
직접 호출하지 말 것.

정책:
    1) 과거 날짜는 서버에서도 수정 불가. UI에서 disabled 처리해도 raw POST로
       우회 가능하므로 서버단 검증이 필요하다.
    2) 이미 마감(finalized)된 DailyPlan이 있는 날짜는 수정 불가. 계획 생성
       이후 가용시간만 바뀌면 DailyPlan.available_minutes와 실제 값이
       어긋나는 데이터 정합성 문제가 생긴다.
    3) 위 두 정책을 통과해 저장된 날짜는, 그 날짜에 이미 DailyPlan이 있으면
       DailyPlan.available_minutes도 같이 동기화한다.
"""
from django.utils import timezone

from planner.models import DailyPlan


class AvailableTimeEditRejected(Exception):
    """과거 날짜 또는 이미 마감된 날짜를 고치려 할 때 발생한다."""

    def __init__(self, rejected_dates):
        self.rejected_dates = rejected_dates
        super().__init__(f"수정할 수 없는 날짜: {rejected_dates}")


def save_available_time_formset(formset, *, exam_period):
    """
    검증된(formset.is_valid() == True) AvailableTimeFormSet을 정책에 따라
    저장하고, 영향받은 날짜의 DailyPlan.available_minutes를 동기화한다.

    Returns:
        저장된 AvailableTime의 날짜 목록

    Raises:
        AvailableTimeEditRejected: 과거 날짜 또는 마감된 날짜가 포함된 경우
            (이 경우 아무것도 저장하지 않는다 - 부분 저장으로 인한 혼란 방지)
    """
    today = timezone.localdate()

    finalized_dates = set(
        DailyPlan.objects.filter(
            exam_period=exam_period, finalized_at__isnull=False,
        ).values_list("date", flat=True)
    )

    rejected = []
    for form in formset.forms:
        date = form.cleaned_data.get("date")
        if date is None:
            continue
        if date < today or date in finalized_dates:
            rejected.append(date)

    if rejected:
        raise AvailableTimeEditRejected(rejected)

    instances = formset.save(commit=False)
    for instance in instances:
        instance.exam_period = exam_period
        instance.save()

    changed_dates = [instance.date for instance in instances]
    if changed_dates:
        available_by_date = {
            instance.date: instance.available_minutes for instance in instances
        }
        daily_plans = DailyPlan.objects.filter(
            exam_period=exam_period, date__in=changed_dates,
        )
        for daily_plan in daily_plans:
            new_minutes = available_by_date[daily_plan.date]
            if daily_plan.available_minutes != new_minutes:
                daily_plan.available_minutes = new_minutes
                daily_plan.save(update_fields=["available_minutes"])

    return changed_dates
