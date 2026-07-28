# exams 앱 잘 연결되는지 보려고 썼어요!! 파일엔 영향 없을거예용!
from django.contrib import admin
from .models import ExamPeriod, Exam, AvailableTime, StudyMaterial, StudyTask

@admin.register(ExamPeriod)
class ExamPeriodAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'user', 'start_date', 'end_date', 'status')
    list_filter = ('status', 'user')

@admin.register(Exam)
class ExamAdmin(admin.ModelAdmin):
    list_display = ('id', 'subject_name', 'exam_period', 'exam_date', 'priority', 'speed_factor')

@admin.register(AvailableTime)
class AvailableTimeAdmin(admin.ModelAdmin):
    list_display = ('id', 'exam_period', 'date', 'available_minutes')

@admin.register(StudyTask)
class StudyTaskAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'exam', 'importance', 'depth', 'difficulty', 'is_confirmed')
    list_filter = ('importance', 'depth', 'difficulty', 'is_confirmed')