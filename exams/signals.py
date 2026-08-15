from django.db import transaction
from django.db.models.signals import post_delete, pre_save
from django.dispatch import receiver

from exams.models import StudyMaterial


@receiver(post_delete, sender=StudyMaterial)
def delete_study_material_file_on_delete(sender, instance, **kwargs):
    if not instance.file:
        return

    file_ = instance.file

    def _delete_file():
        file_.delete(save=False)

    transaction.on_commit(_delete_file)


@receiver(pre_save, sender=StudyMaterial)
def delete_old_file_on_change(sender, instance, **kwargs):
    if not instance.pk:
        return

    try:
        old_file = StudyMaterial.objects.get(pk=instance.pk).file
    except StudyMaterial.DoesNotExist:
        return

    new_file = instance.file
    if old_file and old_file != new_file:
        old_file_ = old_file

        def _delete_old_file():
            old_file_.delete(save=False)

        transaction.on_commit(_delete_old_file)