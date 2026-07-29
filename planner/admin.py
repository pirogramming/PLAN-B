from django.contrib import admin

from .models import (
    DailyPlan,
    DailyPlanItem,
    ProgressLog,
    RecoveryPlan,
    RecoveryPlanItem,
)

admin.site.register(DailyPlan)
admin.site.register(DailyPlanItem)
admin.site.register(ProgressLog)
admin.site.register(RecoveryPlan)
admin.site.register(RecoveryPlanItem)
