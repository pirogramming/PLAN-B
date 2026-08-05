"""
FE1(신예원) 전용 템플릿 태그.

여기 있는 이유:
- subject_create, material_create, task_review 뷰가 '같은 시험기간의 다른 과목 목록'을
  context로 안 내려주고 있어서, Figma처럼 "등록된 과목 리스트"나 "과목 탭 전환"을
  보여주려면 어딘가에서 그 데이터를 가져와야 함.
- views.py를 직접 고치는 대신(BE2 담당 파일), 프론트 표시 목적으로만 쓰는 조회 로직을
  템플릿 태그로 분리해둠. 나중에 백엔드 뷰가 정식으로 context를 내려주게 되면
  이 템플릿 태그 호출부만 지우면 되니까 되돌리기 쉬움.
- 여기서는 절대 데이터를 만들거나 바꾸지 않음(읽기 전용 조회만).
"""
from datetime import timedelta

from django import template
from exams.models import Exam, AvailableTime, ExamPeriod

register = template.Library()


@register.simple_tag
def get_period_exams(period_id):
    """해당 시험기간에 등록된 과목 목록 (시험일 순)."""
    return Exam.objects.filter(exam_period_id=period_id).order_by('exam_date')


@register.simple_tag
def get_calendar_grid(period_id):
    """
    가용시간 입력 화면의 캘린더 그리드용 데이터.

    시험기간 시작일~종료일의 모든 날짜를 순서대로 돌면서,
    - 시험일이면 is_exam_day=True (AvailableTime에 없는 날)
    - 아니면 AvailableTime formset 안에서 몇 번째(index)에 해당하는지 계산

    formset은 AvailableTime.objects.filter(exam_period=period).order_by('date')
    순서와 동일하다고 가정함 (모델 Meta.ordering = ['date']).
    """
    period = ExamPeriod.objects.get(pk=period_id)
    exam_dates = set(
        Exam.objects.filter(exam_period_id=period_id).values_list('exam_date', flat=True)
    )
    available_dates = list(
        AvailableTime.objects.filter(exam_period_id=period_id)
        .order_by('date')
        .values_list('date', flat=True)
    )
    date_to_index = {d: i for i, d in enumerate(available_dates)}

    days = []
    current = period.start_date
    while current <= period.end_date:
        days.append({
            'date': current,
            'weekday': current.isoweekday(),  # 1=월 ... 7=일
            'is_exam_day': current in exam_dates,
            'formset_index': date_to_index.get(current),
        })
        current += timedelta(days=1)
    return days