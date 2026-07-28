from django import forms
from .models import ExamPeriod, AvailableTime, Exam, StudyMaterial, StudyTask


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


class AvailableTimeForm(forms.ModelForm):
    class Meta:
        model = AvailableTime
        fields = ['date', 'available_minutes']
        widgets = {
            'date': forms.DateInput(attrs={
                'class': 'form-control date-picker',
                'type': 'date'
            }),
            'available_minutes': forms.NumberInput(attrs={
                'class': 'form-control number-input',
                'min': '0',
                'step': '5',
                'placeholder': '분 단위 입력 (예: 240)'
            }),
        }


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
                'placeholder': '텍스트 타입 선택 시 여기에 시험 범위를 입력하세요.'
            }),
        }

    def clean(self):
        cleaned_data = super().clean()
        material_type = cleaned_data.get('material_type')
        file = cleaned_data.get('file')
        extracted_text = cleaned_data.get('extracted_text')

        if material_type == 'TEXT' and not extracted_text:
            self.add_error('extracted_text', '텍스트 입력 방식을 선택한 경우 내용을 입력해야 합니다.')
        elif material_type == 'PDF' and not file:
            self.add_error('file', 'PDF 업로드 방식을 선택한 경우 PDF 파일을 첨부해야 합니다.')

        return cleaned_data


class StudyTaskForm(forms.ModelForm):
    class Meta:
        model = StudyTask
        fields = [
            'unit_name', 'title', 'task_type', 'importance', 
            'depth', 'difficulty', 'estimated_min_minutes', 
            'estimated_max_minutes', 'is_confirmed'
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
            'estimated_min_minutes': forms.NumberInput(attrs={
                'class': 'form-control number-input',
                'min': '0',
                'step': '5',
                'placeholder': '최소 예상시간(분)'
            }),
            'estimated_max_minutes': forms.NumberInput(attrs={
                'class': 'form-control number-input',
                'min': '0',
                'step': '5',
                'placeholder': '최대 예상시간(분)'
            }),
            'is_confirmed': forms.CheckboxInput(attrs={
                'class': 'form-check-input'
            }),
        }