"""
가용시간(AvailableTime) 저장 시 공통으로 지켜야 하는 정책을 한 곳에 모은다.
exams:available_time_update(공용 화면)와 exams:period_manage_available_time
(관리 화면)이 같이 쓴다 — 이 파일을 거치지 않고 AvailableTimeFormSet.save()를
직접 호출하지 말 것.

정책:
    1) formset에는 화면에 보이는 모든 날짜가 항상 같이 실려 오므로, 값이
       실제로 바뀐 폼만 검사·저장 대상으로 본다 (안 바뀐 과거/마감 날짜가
       같이 있어도 막지 않는다).
    2) 바뀐 날짜 중 과거 날짜, 이미 마감(finalized)된 DailyPlan이 있는 날짜,
       또는 이미 배정된 시간(DailyPlan.planned_minutes)보다 더 적게 줄이려는
       날짜가 하나라도 있으면 전체 저장을 취소한다 (부분 저장으로 인한 혼란을
       방지 - 사용자가 어떤 날짜는 저장되고 어떤 날짜는 안 됐는지 헷갈리지
       않게). 배정된 시간보다 적게 줄이는 경우를 막는 이유: 가용시간이 줄어든
       만큼 계획을 자동으로 재배치/축소하는 건 작업 순서·복구 로직까지 영향이
       번지는 별도 책임 범위라 (#147), 이 API에서는 다루지 않는다.
    3) 저장에 성공한 날짜 중 이미 DailyPlan이 있는 날짜는
       DailyPlan.available_minutes도 같이 동기화한다.
"""
from django.db import transaction
from django.utils import timezone

from planner.models import DailyPlan


class AvailableTimeEditRejected(Exception):
    """변경하려는 값 중 과거/마감된 날짜, 또는 배정된 시간보다 적게
    줄이려는 날짜가 있을 때 발생한다."""

    def __init__(self, blocked_dates):
        # [(date, "past" | "finalized" | "below_planned", planned_minutes | None), ...]
        self.blocked_dates = blocked_dates
        super().__init__(f"수정할 수 없는 날짜: {blocked_dates}")


def blocked_date_messages(blocked_dates):
    """AvailableTimeEditRejected.blocked_dates -> 사용자에게 보여줄 문구 목록"""
    messages = []
    for blocked_date, reason, planned_minutes in blocked_dates:
        if reason == "past":
            messages.append(f"{blocked_date} 은(는) 지난 날짜라 가용 시간을 수정할 수 없습니다.")
        elif reason == "finalized":
            messages.append(f"{blocked_date} 은(는) 이미 마감된 날짜라 가용 시간을 수정할 수 없습니다.")
        else:
            messages.append(
                f"{blocked_date} 은(는) 이미 {planned_minutes}분이 배정되어 있어 "
                f"그보다 적게 줄일 수 없습니다."
            )
    return messages


def save_available_time_formset(formset, *, exam_period):
    """
    검증된(formset.is_valid() == True) AvailableTimeFormSet에서 실제로 값이
    바뀐 폼만 골라 정책에 따라 저장하고, 영향받은 날짜의
    DailyPlan.available_minutes를 동기화한다.

    Returns:
        저장된 AvailableTime 인스턴스 목록 (변경 없음 -> 빈 리스트)

    Raises:
        AvailableTimeEditRejected: 바뀐 값 중 과거/마감된 날짜, 또는 이미
            배정된 시간보다 적게 줄이려는 날짜가 있는 경우
            (이 경우 아무것도 저장하지 않는다)
    """
    today = timezone.localdate()

    daily_plans_by_date = {
        daily_plan.date: daily_plan
        for daily_plan in DailyPlan.objects.filter(exam_period=exam_period)
    }

    changed_forms = []
    blocked_dates = []

    for form in formset.forms:
        if not form.cleaned_data:
            continue

        form_date = form.cleaned_data.get("date")
        if form_date is None:
            continue

        hours = form.cleaned_data.get("hours") or 0
        minutes = form.cleaned_data.get("minutes") or 0
        submitted_minutes = hours * 60 + minutes

        instance = form.instance
        current_minutes = (
            instance.available_minutes if instance and instance.pk else 0
        ) or 0

        if submitted_minutes == current_minutes:
            continue

        changed_forms.append(form)

        daily_plan = daily_plans_by_date.get(form_date)
        if form_date < today:
            blocked_dates.append((form_date, "past", None))
        elif daily_plan is not None and daily_plan.finalized_at is not None:
            blocked_dates.append((form_date, "finalized", None))
        elif daily_plan is not None and submitted_minutes < daily_plan.planned_minutes:
            blocked_dates.append((form_date, "below_planned", daily_plan.planned_minutes))

    if blocked_dates:
        raise AvailableTimeEditRejected(blocked_dates)

    saved_instances = []
    with transaction.atomic():
        for form in changed_forms:
            instance = form.save(commit=False)
            instance.exam_period = exam_period
            instance.save()
            saved_instances.append(instance)

        if saved_instances:
            affected_plans = []
            for instance in saved_instances:
                daily_plan = daily_plans_by_date.get(instance.date)
                if daily_plan is not None:
                    daily_plan.available_minutes = instance.available_minutes
                    affected_plans.append(daily_plan)
            if affected_plans:
                DailyPlan.objects.bulk_update(affected_plans, ["available_minutes"])

    return saved_instances
