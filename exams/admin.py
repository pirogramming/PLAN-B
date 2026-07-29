from django.contrib import admin
from .models import ExamPeriod, AvailableTime, Exam, StudyMaterial, StudyTask


class AvailableTimeInline(admin.TabularInline):
    """ExamPeriod 수정 페이지에서 가용 시간 목록을 함께 확인/수정"""
    model = AvailableTime
    extra = 0
    readonly_fields = ('date', 'available_minutes')
    can_delete = False


@admin.register(ExamPeriod)
class ExamPeriodAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'title', 'start_date', 'end_date', 'created_at')
    list_filter = ('start_date', 'end_date')
    search_fields = ('title', 'user__username', 'user__email')
    inlines = [AvailableTimeInline]


@admin.register(AvailableTime)
class AvailableTimeAdmin(admin.ModelAdmin):
    list_display = ('id', 'exam_period', 'date', 'available_minutes')
    list_filter = ('date',)
    search_fields = ('exam_period__title',)


class StudyMaterialInline(admin.TabularInline):
    """Exam 수정 페이지에서 등록된 학습 자료를 함께 확인"""
    model = StudyMaterial
    extra = 0


@admin.register(Exam)
class ExamAdmin(admin.ModelAdmin):
    list_display = ('id', 'exam_period', 'subject_name', 'exam_date', 'priority')
    list_filter = ('priority', 'exam_date')
    search_fields = ('subject_name', 'exam_period__title')
    inlines = [StudyMaterialInline]


@admin.register(StudyMaterial)
class StudyMaterialAdmin(admin.ModelAdmin):
    list_display = ('id', 'exam', 'title', 'material_type', 'created_at')
    list_filter = ('material_type', 'created_at')
    search_fields = ('title', 'exam__name')


@admin.register(StudyTask)
class StudyTaskAdmin(admin.ModelAdmin):
    list_display = (
        'id', 
        'study_material', 
        'unit_name', 
        'title', 
        'importance', 
        'difficulty', 
        'is_confirmed', 
        'is_user_modified'
    )
    list_filter = ('is_confirmed', 'is_user_modified', 'importance', 'difficulty', 'task_type')
    search_fields = ('unit_name', 'title', 'study_material__title')
    readonly_fields = ('estimated_min_minutes', 'estimated_max_minutes')