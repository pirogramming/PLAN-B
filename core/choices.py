from django.db import models


class ExamPeriodStatus(models.TextChoices):
    DRAFT = 'draft', '작성 중'
    ACTIVE = 'active', '진행 중'
    COMPLETED = 'completed', '완료'
    ARCHIVED = 'archived', '보관'


class PriorityLevel(models.TextChoices):
    HIGH = 'high', '높음'
    MEDIUM = 'medium', '보통'
    LOW = 'low', '낮음'


class TaskDepth(models.TextChoices):
    CORE = 'core', '핵심'
    BASIC = 'basic', '기본'
    OPTIONAL = 'optional', '선택'


class TaskDifficulty(models.TextChoices):
    EASY = 'easy', '쉬움'
    NORMAL = 'normal', '보통'
    HARD = 'hard', '어려움'


class TaskType(models.TextChoices):
    CONCEPT = 'concept', '개념 학습'
    PRACTICE = 'practice', '문제 풀이'
    REVIEW = 'review', '복습'
    SUMMARY = 'summary', '요약 정리'
    CUSTOM = 'custom', '기타/직접 입력'


class MaterialType(models.TextChoices):
    PDF = 'pdf', 'PDF 파일'
    TEXT = 'text', '텍스트 직접 입력'


class MaterialStatus(models.TextChoices):
    PENDING = 'pending', '대기 중'
    PROCESSING = 'processing', '처리 중'
    COMPLETED = 'completed', '완료'
    FAILED = 'failed', '실패'

class DailyPlanStatus(models.TextChoices):
    PLANNED = 'PLANNED', '계획됨'
    IN_PROGRESS = 'IN_PROGRESS', '진행중'
    COMPLETED = 'COMPLETED', '완료'
    AT_RISK = 'AT_RISK', '위험'


class ProgressStatus(models.TextChoices):
    DONE = 'DONE', '완료'
    PARTIAL = 'PARTIAL', '일부완료'
    NOT_DONE = 'NOT_DONE', '못함'


class RecoveryType(models.TextChoices):
    MAINTAIN_VOLUME = 'MAINTAIN_VOLUME', '분량 유지형'
    CORE_FOCUS = 'CORE_FOCUS', '핵심 집중형'


class RecoveryPlanStatus(models.TextChoices):
    PENDING = 'PENDING', '선택 대기'
    APPLIED = 'APPLIED', '적용됨'
    DISCARDED = 'DISCARDED', '미채택'


class RecoveryActionType(models.TextChoices):
    RESCHEDULE = 'RESCHEDULE', '재배치'
    EXCLUDE = 'EXCLUDE', '제외'
