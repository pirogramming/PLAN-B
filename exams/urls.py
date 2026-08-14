from django.urls import path
from . import views

app_name = 'exams'

urlpatterns = [
    # ── 표에 있는 것 ──────────────────────────
    path('periods/', views.period_list, name='period_list'),
    path('periods/create/', views.period_create, name='period_create'),
    path('periods/<int:period_id>/', views.period_detail, name='period_detail'),
    path('periods/<int:period_id>/manage/', views.period_manage, name='period_manage'),
    path('periods/<int:period_id>/manage/available-time/', views.period_manage_available_time, name='period_manage_available_time'),
    path('subjects/<int:exam_id>/tasks/view/', views.period_manage_task_view, name='period_manage_task_view'),
    path('periods/<int:period_id>/subjects/create/', views.subject_create, name='subject_create'),
    path('periods/<int:period_id>/available-time/', views.available_time_update, name='available_time_update'),
    path('subjects/<int:exam_id>/materials/create/', views.material_create, name='material_create'),
    path('materials/<int:material_id>/', views.material_detail, name='material_detail'),
    path('subjects/<int:exam_id>/tasks/review/', views.task_review, name='task_review'),

    # ── 표에 없어서 임시로 지음 (이주헌/신예원에게 공유 필요) ──
    path('periods/<int:period_id>/update/', views.period_update, name='period_update'),
    path('periods/<int:period_id>/delete/', views.period_delete, name='period_delete'),
    path('periods/<int:period_id>/subjects/<int:exam_id>/update/', views.subject_update, name='subject_update'),
    path('periods/<int:period_id>/subjects/<int:exam_id>/delete/', views.subject_delete, name='subject_delete'),
    path('materials/<int:material_id>/extract/', views.material_extract, name='material_extract'),
    path('materials/<int:material_id>/analyze/', views.material_analyze, name='material_analyze'),
    path('materials/<int:material_id>/analyze/retry/', views.material_retry_analyze, name='material_retry_analyze'),
    path('materials/<int:material_id>/analyze/status/', views.material_analysis_status, name='material_analysis_status'),
    path('materials/<int:material_id>/delete/', views.material_delete, name='material_delete'),
    path('subjects/<int:exam_id>/tasks/create/', views.study_task_create, name='task_create'),
    path('subjects/<int:exam_id>/tasks/confirm/', views.study_task_confirm, name='task_confirm'),
]