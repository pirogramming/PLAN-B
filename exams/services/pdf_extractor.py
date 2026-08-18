import io
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import pypdfium2 as pdfium

try:
    import pytesseract
    from pytesseract import Output
    from PIL import Image, ImageOps, ImageFilter
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

logger = logging.getLogger(__name__)


class PdfExtractionError(Exception):
    """PDF 텍스트 추출 과정에서 발생하는 예외를 표준화.

    user_message: 사용자에게 그대로 노출해도 안전한, 원인을 카테고리 수준으로
    설명하는 일반화된 문구. 파일 경로, 스토리지 백엔드 오류 문자열, 라이브러리
    내부 예외 메시지 등 구현 세부사항은 포함하지 않는다.

    detail: 로그/DB(error_message)에 저장할 상세 원인. 원본 예외(str(e))를
    그대로 담을 수 있어 내부 경로/스토리지 오류 등이 포함될 수 있으므로,
    화면에 노출하는 용도로는 절대 쓰지 않는다.
    """
    def __init__(self, user_message: str, detail: str | None = None):
        self.user_message = user_message
        self.detail = detail if detail is not None else user_message
        super().__init__(self.detail)


# =====================================================================
# PDFium(pypdfium2) 호출 직렬화용 락
#
# pypdfium2가 감싸는 PDFium 라이브러리는 thread-safe하지 않다. 여러 자료를
# 동시에 추출할 때 ThreadPoolExecutor(OCR 병렬 처리) 안에서 여러 스레드가
# PdfDocument 생성/page.render()를 동시에 호출하면 내부 상태가 꼬여
# "PDFium: Data format error"가 발생하고, 한 번 꼬이면 프로세스를 재시작하기
# 전까지 같은 PDF를 단건으로 재시도해도 계속 실패한다 (전역 상태 오염).
#
# 그래서 PDFium 호출(문서 열기/페이지 접근/렌더링/닫기) 전체를 이 락으로
# 직렬화한다. Tesseract OCR(pytesseract)과 PIL 전처리는 PDFium과 무관하고
# thread-safe하므로 이 락 밖에서 그대로 병렬 처리한다 - 그래야
# ThreadPoolExecutor(max_workers=4)의 이점을 유지할 수 있다.
#
# 워커 개수(ocr_max_workers)를 1로 낮추면 이 문제가 사라지는 것도 같은
# 이유다(경합 자체가 없어지므로) - 하지만 그러면 OCR 병렬성을 전부 잃는다.
# 이 락은 "PDFium 호출만" 직렬화해 그 비용 없이 문제를 해결한다.
# =====================================================================
_PDFIUM_LOCK = threading.Lock()


# 정상적인 한글/영문/숫자/기본 문장부호/공백으로 간주할 문자 범위
_NORMAL_CHAR_PATTERN = re.compile(
    r'[가-힣ㄱ-ㅎㅏ-ㅣa-zA-Z0-9\s.,!?():;\'"\-_+=*/<>\[\]{}%&#@~^|\\]'
)


def _anomaly_ratio(text: str) -> float:
    """
    텍스트 중 '정상적이지 않은' 문자(도식/노이즈가 문자로 오인식된 경우,
    또는 폰트 매핑이 깨져 엉뚱한 코드포인트로 추출된 경우 등)의 비율.
    0에 가까울수록 깨끗한 텍스트, 1에 가까울수록 쓰레기 문자열.
    OCR 결과뿐 아니라 텍스트 레이어 추출 결과에도 동일하게 적용한다
    (커스텀 폰트 서브셋이 깨진 PDF는 텍스트 레이어가 있어도 글자가 깨져 나오는 경우가 있음).
    """
    if not text:
        return 1.0
    stripped = text.replace(" ", "").replace("\n", "")
    if not stripped:
        return 1.0
    normal_count = len(_NORMAL_CHAR_PATTERN.findall(stripped))
    return 1.0 - (normal_count / len(stripped))


def _preprocess_image(img: "Image.Image") -> "Image.Image":
    """OCR 정확도 향상을 위한 기본 전처리 (그레이스케일 → 오토콘트라스트 → 샤픈 → 이진화)"""
    gray = img.convert("L")
    gray = ImageOps.autocontrast(gray, cutoff=1)
    gray = gray.filter(ImageFilter.SHARPEN)
    return gray.point(lambda p: 255 if p > 150 else 0)


def _ocr_with_confidence(img: "Image.Image", lang: str) -> tuple[str, float]:
    """이미지를 OCR하고 (텍스트, 평균 confidence) 반환. PDFium과 무관 - 락 불필요."""
    try:
        data = pytesseract.image_to_data(img, lang=lang, output_type=Output.DICT)
    except Exception as e:
        logger.warning(f"OCR 데이터 추출 실패: {e}")
        return "", 0.0

    words, confidences = [], []
    for text, raw_conf in zip(data["text"], data["conf"]):
        text = text.strip()
        try:
            conf = float(raw_conf)
        except (ValueError, TypeError):
            continue
        if text and conf >= 0:
            words.append(text)
            confidences.append(conf)

    full_text = " ".join(words).strip()
    avg_conf = sum(confidences) / len(confidences) if confidences else 0.0
    return full_text, avg_conf


def _ocr_title_region(raw_img: "Image.Image", lang: str, top_ratio: float = 0.18) -> str:
    """
    페이지 상단 top_ratio(기본 18%) 영역만 잘라서 별도로 OCR.
    제목/표지는 본문보다 정보 가치가 높아서 우선 확보하는 목적.
    실패해도 전체 파이프라인에 영향 없도록 예외를 삼킴.
    PDFium과 무관(이미 렌더링된 PIL 이미지를 crop) - 락 불필요.
    """
    try:
        w, h = raw_img.size
        title_crop = raw_img.crop((0, 0, w, int(h * top_ratio)))
        title_text, title_conf = _ocr_with_confidence(title_crop, lang)
        if not title_text.strip():
            pre_crop = _preprocess_image(title_crop)
            title_text, title_conf = _ocr_with_confidence(pre_crop, lang)
        return title_text.strip()
    except Exception as e:
        logger.warning(f"제목 영역 OCR 실패: {e}")
        return ""


def _ocr_page(
    pdf_bytes: bytes,
    page_index: int,
    lang: str,
    dpi: int,
    conf_threshold: float,
    min_text_len: int,
    anomaly_threshold: float,
    extract_title: bool,
) -> tuple[int, str, float, float]:
    """
    페이지 하나를 렌더링 후 OCR.
    1차: 원본 이미지 전체 OCR
    2차: confidence 낮거나 이상문자 비율 높으면 전처리 후 재시도, 더 나은 쪽 채택
    제목 영역은 별도로 OCR해서 본문 앞에 붙임.
    반환: (page_index, text, final_conf, final_anomaly_ratio)

    이 함수는 ThreadPoolExecutor(max_workers=4)의 워커 스레드에서 동시에
    여러 개가 실행된다. PDFium 호출(PdfDocument 생성 ~ render ~ close)만
    _PDFIUM_LOCK으로 직렬화하고, 그 뒤의 OCR(pytesseract)/전처리(PIL)는
    락 밖에서 그대로 병렬 실행되게 한다 - 병렬성을 잃는 부분은 PDFium
    렌더링 자체뿐이고, 보통 OCR/전처리보다 훨씬 빠르므로 전체 처리 시간에는
    영향이 크지 않다.
    """
    pdf = None
    page = None
    raw_img = None
    try:
        with _PDFIUM_LOCK:
            pdf = pdfium.PdfDocument(pdf_bytes)
            page = pdf[page_index]
            scale = dpi / 72
            bitmap = page.render(scale=scale)
            raw_img = bitmap.to_pil()
    except Exception as e:
        logger.warning(f"페이지 렌더링 실패 (page {page_index + 1}): {e}")
        return page_index, "", 0.0, 1.0
    finally:
        with _PDFIUM_LOCK:
            if page is not None:
                try:
                    page.close()
                except Exception as e:
                    logger.warning(f"페이지 핸들 닫기 실패 (page {page_index + 1}): {e}")
            if pdf is not None:
                try:
                    pdf.close()
                except Exception as e:
                    logger.warning(f"PDF 핸들 닫기 실패 (page {page_index + 1}): {e}")

    # ---- 여기서부터는 PDFium과 무관 (OCR/전처리) → 락 없이 병렬 실행 ----

    # 1차: 원본 전체 OCR
    raw_text, raw_conf = _ocr_with_confidence(raw_img, lang)
    raw_anomaly = _anomaly_ratio(raw_text)

    is_good_enough = (
        raw_conf >= conf_threshold
        and len(raw_text) >= min_text_len
        and raw_anomaly <= anomaly_threshold
    )

    if is_good_enough:
        final_text, final_conf, final_anomaly = raw_text, raw_conf, raw_anomaly
        logger.info(
            f"page {page_index + 1}: 원본 OCR 채택 "
            f"(conf={raw_conf:.1f}, anomaly={raw_anomaly:.2f})"
        )
    else:
        logger.info(
            f"page {page_index + 1}: 원본 OCR 결과 부족 "
            f"(conf={raw_conf:.1f}, len={len(raw_text)}, anomaly={raw_anomaly:.2f}) → 전처리 재시도"
        )
        pre_img = _preprocess_image(raw_img)
        pre_text, pre_conf = _ocr_with_confidence(pre_img, lang)
        pre_anomaly = _anomaly_ratio(pre_text)

        pre_is_better = (
            pre_conf > raw_conf + 5
            or pre_anomaly < raw_anomaly - 0.05
        )
        if pre_is_better:
            final_text, final_conf, final_anomaly = pre_text, pre_conf, pre_anomaly
            logger.info(
                f"page {page_index + 1}: 전처리 OCR 채택 "
                f"(conf={pre_conf:.1f}, anomaly={pre_anomaly:.2f})"
            )
        else:
            final_text, final_conf, final_anomaly = raw_text, raw_conf, raw_anomaly
            logger.info(
                f"page {page_index + 1}: 전처리해도 개선 없어 원본 유지 "
                f"(conf={raw_conf:.1f}, anomaly={raw_anomaly:.2f})"
            )

    if extract_title:
        title_text = _ocr_title_region(raw_img, lang)
        if title_text and title_text not in final_text[:len(title_text) + 20]:
            final_text = f"[제목] {title_text}\n{final_text}"

    return page_index, final_text, final_conf, final_anomaly


def extract_text_from_pdf(
    file_field,
    ocr_lang: str = "kor+eng",
    ocr_dpi: int = 300,
    min_chars_per_page: int = 5,
    ocr_max_workers: int = 4,
    ocr_conf_threshold: float = 60.0,
    ocr_min_text_len: int = 10,
    ocr_anomaly_threshold: float = 0.25,
    ocr_extract_title: bool = True,
) -> str:
    """
    Django FileField에서 텍스트를 추출한다.

    - 암호화된 PDF는 pypdfium2가 open() 시점에 빈 암호로 자동 시도, 실패 시 PdfiumError.
      암호화 여부 판별은 예외 메시지 문자열(password/encrypt) 매칭에 의존하므로,
      requirements.txt에서 pypdfium2 버전을 정확히 고정(pin)해서 사용해야 한다.
      버전을 올릴 때는 tests.py의 test_extract_text_encrypted_not_supported가
      여전히 통과하는지 반드시 확인할 것.
    - 페이지별로 텍스트 레이어를 우선 확인한다. 텍스트가 있어도 이상문자 비율이 높으면
      OCR로 폴백한다. 단, 이 경우 텍스트 레이어 결과를 완전히 버리지 않고 fallback으로
      들고 있다가, OCR이 불가능하거나(라이브러리 미설치) OCR 결과가 실제로 비어있는
      경우에는 (깨졌더라도) 원본 텍스트 레이어 결과를 그대로 사용한다 — 아무 것도
      안 남기는 것보다는, 이상 문자가 섞여 있더라도 원본 텍스트가 있는 편이 낫다.
    - OCR: confidence + 이상문자 비율(anomaly_ratio) 둘 다 나쁠 때만 전처리 재시도
    - 슬라이드 상단 영역은 별도 OCR로 제목을 우선 확보해 본문 앞에 덧붙임
    - 페이지 경계를 "--- 페이지 N ---" 마커로 표시해 출처 추적 가능

    PDFium(pypdfium2)은 thread-safe하지 않다. 이 함수 자체는 메인 스레드에서
    실행되지만, 동시에 여러 자료를 추출하는 다른 요청/스레드의 PDFium 호출과
    부딪힐 수 있어(전역 상태 오염 → 한 번 깨지면 프로세스 재시작 전까지 계속
    실패) 이 함수 안의 모든 PDFium 호출(문서 열기, 페이지 텍스트 레이어 추출,
    닫기)도 _ocr_page와 동일한 _PDFIUM_LOCK으로 직렬화한다.
    """
    try:
        file_field.open('rb')
        file_field.seek(0)
        pdf_bytes = file_field.read()
    except Exception as e:
        # e에는 스토리지 백엔드(S3/로컬 파일시스템 등)의 내부 경로나 오류 문자열이
        # 담길 수 있어 사용자 메시지로는 노출하지 않는다. detail에만 원본을 남긴다.
        raise PdfExtractionError("파일을 읽을 수 없습니다.", detail=f"파일 읽기 실패: {e}") from e
    finally:
        try:
            file_field.close()
        except Exception as e:
            logger.warning(f"파일을 닫는 중 오류 발생: {e}")

    try:
        with _PDFIUM_LOCK:
            pdf = pdfium.PdfDocument(pdf_bytes)
    except pdfium.PdfiumError as e:
        msg = str(e).lower()
        if "password" in msg or "encrypt" in msg:
            raise PdfExtractionError("암호화된 PDF 파일은 지원하지 않습니다.", detail=str(e)) from e
        raise PdfExtractionError(
            "올바른 PDF 형식이 아니거나 손상된 파일입니다.", detail=str(e)
        ) from e

    try:
        with _PDFIUM_LOCK:
            n_pages = len(pdf)
        page_texts: list[str] = [""] * n_pages
        ocr_needed: list[int] = []
        # anomaly 때문에 OCR로 넘어간 페이지의 원본 텍스트 레이어 결과.
        # OCR이 실패/비활성이거나 빈 결과를 낼 경우 이걸로 되돌린다.
        fallback_texts: dict[int, str] = {}

        for index in range(n_pages):
            try:
                with _PDFIUM_LOCK:
                    page = pdf[index]
                    textpage = page.get_textpage()
                    text = textpage.get_text_range().strip()
                    textpage.close()
                    page.close()
            except Exception as e:
                logger.warning(f"PDF {index + 1}페이지 텍스트 추출 실패: {e}")
                text = ""

            text_anomaly = _anomaly_ratio(text) if text else 1.0
            is_text_too_short = len(text) < min_chars_per_page
            is_text_corrupted = bool(text) and text_anomaly > ocr_anomaly_threshold

            if is_text_too_short or is_text_corrupted:
                if is_text_corrupted and not is_text_too_short:
                    logger.info(
                        f"page {index + 1}: 텍스트 레이어는 있으나 이상문자 비율 높음 "
                        f"(anomaly={text_anomaly:.2f}) → OCR 폴백 (원본은 fallback으로 보존)"
                    )
                    fallback_texts[index] = text
                ocr_needed.append(index)
            else:
                page_texts[index] = text

        page_conf_log: dict[int, tuple[float, float]] = {}

        if ocr_needed:
            if not OCR_AVAILABLE:
                logger.warning(
                    f"{len(ocr_needed)}개 페이지에 텍스트 레이어가 부족하거나 손상되었지만 "
                    "OCR 라이브러리가 설치되어 있지 않아 건너뜁니다."
                )
                # OCR을 아예 못 돌리는 경우, anomaly로 넘어온 페이지는 원본이라도 살린다.
                for idx, fallback in fallback_texts.items():
                    page_texts[idx] = fallback
            else:
                logger.info(f"{len(ocr_needed)}개 페이지 OCR 처리 시작: {[i + 1 for i in ocr_needed]}")
                # PDFium 호출(_ocr_page 내부)만 _PDFIUM_LOCK으로 직렬화되고,
                # Tesseract OCR/PIL 전처리는 여기서 그대로 병렬 실행된다.
                with ThreadPoolExecutor(max_workers=ocr_max_workers) as executor:
                    futures = [
                        executor.submit(
                            _ocr_page, pdf_bytes, idx, ocr_lang, ocr_dpi,
                            ocr_conf_threshold, ocr_min_text_len,
                            ocr_anomaly_threshold, ocr_extract_title,
                        )
                        for idx in ocr_needed
                    ]
                    for future in as_completed(futures):
                        idx, text, conf, anomaly = future.result()

                        if text.strip():
                            # OCR이 뭔가 유효한 결과를 냈으면 그걸 채택
                            page_texts[idx] = text
                        elif idx in fallback_texts:
                            # OCR이 완전히 빈 결과를 냈다면, 깨졌더라도 원본 텍스트 레이어로 복원
                            logger.info(
                                f"page {idx + 1}: OCR 결과가 비어있어 원본 텍스트 레이어로 복원 "
                                f"(anomaly 높지만 완전 공백보다는 나음)"
                            )
                            page_texts[idx] = fallback_texts[idx]
                        # 둘 다 없으면 page_texts[idx]는 빈 문자열 그대로 유지

                        page_conf_log[idx] = (conf, anomaly)

                if page_conf_log:
                    avg_conf = sum(c for c, _ in page_conf_log.values()) / len(page_conf_log)
                    avg_anomaly = sum(a for _, a in page_conf_log.values()) / len(page_conf_log)
                    logger.info(
                        f"OCR 페이지 평균 conf={avg_conf:.1f}, 평균 anomaly_ratio={avg_anomaly:.2f} "
                        f"(임계값: conf>={ocr_conf_threshold}, anomaly<={ocr_anomaly_threshold})"
                    )

        text_parts = [
            f"--- 페이지 {i + 1} ---\n{text}"
            for i, text in enumerate(page_texts)
            if text.strip()
        ]
        full_text = "\n\n".join(text_parts).strip()

        if not full_text:
            detail = (
                "OCR도 실패했습니다." if OCR_AVAILABLE and ocr_needed
                else "스캔된 이미지 PDF이거나 내용이 비어있으며, OCR 라이브러리가 설치되어 있지 않습니다."
            )
            raise PdfExtractionError(
                "PDF에서 텍스트를 추출할 수 없습니다.", detail=detail,
            )

        return full_text

    finally:
        with _PDFIUM_LOCK:
            pdf.close()