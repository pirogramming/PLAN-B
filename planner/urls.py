from django.urls import path
from . import views

app_name = 'planner'

urlpatterns = [
    path('feasibility/<int:period_id>/', views.feasibility, name='feasibility'),
    path('plan/generate/<int:period_id>/', views.plan_generate, name='plan_generate'),
    path('plan/complete/<int:period_id>/', views.plan_complete, name='plan_complete'),
    path('today/', views.today, name='today'),
    path('daily-plans/finalize/', views.daily_plan_finalize, name='daily_plan_finalize'),
    path('', views.dashboard, name='dashboard'),
    path('progress/<int:item_id>/', views.progress_record, name='progress_record'),
    path("recovery/<uuid:group_id>/", views.recovery_compare, name="recovery_compare"),
    path("recovery/<int:plan_id>/preview/", views.recovery_preview, name="recovery_preview"),
    path("recovery/<int:plan_id>/apply/", views.recovery_apply, name="recovery_apply"),
]