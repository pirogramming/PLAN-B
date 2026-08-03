"""
프로젝트 공통 예외 정의
"""


class PlanBBaseException(Exception):
    """PLAN B 서비스 로직 전반에서 사용하는 기본 예외"""
    pass


class AIAnalysisError(PlanBBaseException):
    """
    AI 분석(단원 분리·학습 작업 생성) 과정에서 발생하는 예외의 기본 클래스.
    (exams/services/task_extractor.py)
    """
    pass


class AICallFailedError(AIAnalysisError):
    """AI API 호출 자체가 실패했을 때 (네트워크 오류, 인증 오류, 타임아웃 등)"""
    pass


class AIResponseValidationError(AIAnalysisError):
    """AI 응답이 기대한 JSON 스키마·choices 값을 따르지 않을 때"""
    pass