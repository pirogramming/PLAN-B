import os
from django import forms
from django.forms import modelformset_factory, BaseModelFormSet
from django.core.exceptions import ValidationError
from core.choices import MaterialType
from .models import ExamPeriod, AvailableTime, Exam, StudyMaterial, StudyTask

MAX_UPLOAD_SIZE = 20 * 1024 * 1024

class ExamPeriodForm(forms.ModelForm):
    class Meta:
        model = ExamPeriod
        fields = ['title', 'start_date', 'end_date']
        widgets = {
            'title': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': '예: 2026년 2학기 중간고사'
            }),
            'start_date': forms.DateInput(attrs={
                'class': 'form-control date-picker',
                'type': 'date'
            }),
            'end_date': forms.DateInput(attrs={
                'class': 'form-control date-picker',
                'type': 'date'
            }),
        }

    def clean(self):
        cleaned_data = super().clean()
        start_date = cleaned_data.get('start_date')
        end_date = cleaned_data.get('end_date')

        if start_date and end_date:
            if start_date > end_date:
                raise ValidationError("시작일은 종료일보다 이전이거나 같아야 합니다.")
        return cleaned_data


class AvailableTimeForm(forms.ModelForm):
    hours = forms.IntegerField(
        min_value=0,
        initial=0,
        required=False,
        widget=forms.NumberInput(attrs={
            'class': 'form-control number-input',
            'placeholder': '시간',
            'min': '0'
        }),
        label="시간"
    )
    minutes = forms.IntegerField(
        min_value=0,
        max_value=59,
        initial=0,
        required=False,
        widget=forms.NumberInput(attrs={
            'class': 'form-control number-input',
            'placeholder': '분',
            'min': '0',
            'max': '59',
            'step': '1'
        }),
        label="분"
    )

    class Meta:
        model = AvailableTime
        fields = ['date'] 
        widgets = {
            'date': forms.DateInput(attrs={
                'class': 'form-control-plaintext date-picker',
                'type': 'date',
                'readonly': 'readonly'
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        total_minutes = 0
        if self.instance and self.instance.pk:
            total_minutes = self.instance.available_minutes or 0
        elif 'initial' in kwargs and 'available_minutes' in kwargs['initial']:
            total_minutes = kwargs['initial']['available_minutes'] or 0

        if total_minutes:
            self.fields['hours'].initial = total_minutes // 60
            self.fields['minutes'].initial = total_minutes % 60

    def save(self, commit=True):
        instance = super().save(commit=False)
        hours = self.cleaned_data.get('hours') or 0
        minutes = self.cleaned_data.get('minutes') or 0
        instance.available_minutes = (hours * 60) + minutes

        if commit:
            instance.save()
        return instance


AvailableTimeFormSet = modelformset_factory(
    AvailableTime,
    form=AvailableTimeForm,
    extra=0,
    can_delete=False
)

class ExamForm(forms.ModelForm):
    class Meta:
        model = Exam
        fields = ['name', 'exam_date', 'priority']
        widgets = {
            'name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': '예: 신호및시스템, 공학수학'
            }),
            'exam_date': forms.DateInput(attrs={
                'class': 'form-control date-picker',
                'type': 'date'
            }),
            'priority': forms.Select(attrs={
                'class': 'form-select'
            }),
        }

    def __init__(self, *args, **kwargs):
        self.exam_period = kwargs.pop('exam_period', None)
        super().__init__(*args, **kwargs)

    def clean_exam_date(self):
        exam_date = self.cleaned_data.get('exam_date')
        if exam_date and self.exam_period:
            start_date = self.exam_period.start_date
            end_date = self.exam_period.end_date

            if not (start_date <= exam_date <= end_date):
                raise ValidationError(
                    f"시험 날짜는 설정한 시험 기간({start_date} ~ {end_date}) 내에 속해야 합니다."
                )

        return exam_date

class StudyMaterialForm(forms.ModelForm):
    class Meta:
        model = StudyMaterial
        fields = ['title', 'material_type', 'file', 'extracted_text']
        widgets = {
            'title': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': '예: 1~3장 개념 정리본'
            }),
            'material_type': forms.Select(attrs={
                'class': 'form-select'
            }),
            'file': forms.FileInput(attrs={
                'class': 'form-control file-input',
                'accept': '.pdf'
            }),
            'extracted_text': forms.Textarea(attrs={
                'class': 'form-control textarea-input',
                'rows': 5,
                'placeholder': '텍스트 입력 방식을 선택한 경우, 여기에 학습 범위를 직접 입력해 주세요.'
            }),
        }
        labels = {
            'extracted_text': '학습 범위 텍스트',
            'file': 'PDF 첨부파일',
        }

    def clean_file(self):
        file = self.cleaned_data.get('file')
        if file:
            # 1. .pdf 만 허용
            ext = os.path.splitext(file.name)[1].lower()
            if ext != '.pdf':
                raise ValidationError("PDF 파일(.pdf)만 업로드할 수 있습니다.")

            # 2. 파일 크기 검증 (최대 20MB)
            if file.size > MAX_UPLOAD_SIZE:
                raise ValidationError("파일 크기는 최대 20MB를 초과할 수 없습니다.")

        return file

    def clean(self):
        cleaned_data = super().clean()
        material_type = cleaned_data.get('material_type')
        file = cleaned_data.get('file')
        extracted_text = cleaned_data.get('extracted_text')

        # MaterialType Enum 및 문자열 처리
        is_text_type = material_type in [MaterialType.TEXT, 'TEXT', 'text']
        is_pdf_type = material_type in [MaterialType.PDF, 'PDF', 'pdf']

        # 입력 유형별 필수값 서버 검증
        if is_text_type and not extracted_text:
            self.add_error('extracted_text', '텍스트 입력 방식을 선택한 경우 내용을 입력해야 합니다.')
        elif is_pdf_type and not file:
            self.add_error('file', 'PDF 업로드 방식을 선택한 경우 PDF 파일을 첨부해야 합니다.')

        return cleaned_data


class StudyTaskForm(forms.ModelForm):
    class Meta:
        model = StudyTask
        fields = [
            'unit_name', 
            'title', 
            'task_type', 
            'importance', 
            'depth', 
            'difficulty'
        ]
        widgets = {
            'unit_name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': '예: 1장 푸리에 변환'
            }),
            'title': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': '예: 푸리에 변환 수식 암기 및 예제 풀이'
            }),
            'task_type': forms.Select(attrs={
                'class': 'form-select'
            }),
            'importance': forms.Select(attrs={
                'class': 'form-select'
            }),
            'depth': forms.Select(attrs={
                'class': 'form-select'
            }),
            'difficulty': forms.Select(attrs={
                'class': 'form-select'
            }),
        }

StudyTaskFormSet = modelformset_factory(
    StudyTask,
    form=StudyTaskForm,
    extra=0,
    can_delete=True
)