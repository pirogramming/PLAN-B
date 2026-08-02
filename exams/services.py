import pypdf


class PdfExtractionError(Exception):
    """PDF 텍스트 추출 과정에서 발생하는 예외를 표준화"""
    pass


def extract_text_from_pdf(file_field) -> str:
    """
    Django FileField에서 텍스트를 추출한다.
    - 암호화된 PDF는 지원하지 않음
    - 스캔 이미지 PDF(텍스트 레이어 없음)는 빈 문자열 반환 → 호출부에서 FAILED 처리
    """
    file_field.seek(0)
    try:
        reader = pypdf.PdfReader(file_field)
    except Exception as e:
        raise PdfExtractionError(f"PDF 파일을 열 수 없습니다: {e}")

    if reader.is_encrypted:
        raise PdfExtractionError("암호화된 PDF는 지원하지 않습니다.")

    text_parts = []
    for page in reader.pages:
        page_text = page.extract_text() or ''
        text_parts.append(page_text)

    return "\n".join(text_parts).strip()