import logging
import pypdf

logger = logging.getLogger(__name__)


class PdfExtractionError(Exception):
    """PDF 텍스트 추출 과정에서 발생하는 예외를 표준화"""
    pass


def extract_text_from_pdf(file_field) -> str:
    """
    Django FileField에서 텍스트를 추출한다.
    - 암호화된 PDF는 지원하지 않음
    - 스캔 이미지 PDF(텍스트 레이어 없음)는 PdfExtractionError 발생
    """
    # 1. 파일 열기 및 포인터 초기화
    try:
        file_field.open('rb')
        file_field.seek(0)
    except Exception as e:
        raise PdfExtractionError(f"파일을 읽을 수 없습니다: {e}")

    # 2. PDF Reader 로드
    try:
        reader = pypdf.PdfReader(file_field)
    except Exception as e:
        raise PdfExtractionError(f"올바른 PDF 형식이 아니거나 손상된 파일입니다: {e}")

    # 3. 암호화 체크
    if reader.is_encrypted:
        # pypdf는 빈 암호(공백)로 해제 시도 가능한 경우도 있어 try_decrypt를 체크할 수도 있지만, 
        # 원칙적으로 암호화 파일 거절
        raise PdfExtractionError("암호화된 PDF 파일은 지원하지 않습니다.")

    # 4. 페이지별 텍스트 추출
    text_parts = []
    for index, page in enumerate(reader.pages):
        try:
            page_text = page.extract_text() or ''
            if page_text.strip():
                text_parts.append(page_text.strip())
        except Exception as e:
            logger.warning(f"PDF {index + 1}페이지 텍스트 추출 실패: {e}")
            continue

    full_text = "\n\n".join(text_parts).strip()

    # 5. 스캔 이미지 PDF 등 텍스트가 전혀 안 뽑힌 경우 예외 처리
    if not full_text:
        raise PdfExtractionError("PDF에서 텍스트를 추출할 수 없습니다. (스캔된 이미지 PDF이거나 내용이 비어있습니다.)")

    return full_text