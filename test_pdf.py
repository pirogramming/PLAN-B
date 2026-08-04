import os
import sys
import django

# Django 환경 세팅 (Django 모델 및 settings를 참조하기 위해 필요)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")  # 본인 프로젝트의 settings 위치로 맞추세요 (예: plan_b.settings)
django.setup()

from django.core.files.base import File
from exams.services.pdf_extractor import extract_text_from_pdf, PdfExtractionError


def run_pdf_test(file_path):
    """실제 로컬 PDF 파일 경로를 받아 텍스트 추출 테스트"""
    if not os.path.exists(file_path):
        print(f"❌ 파일을 찾을 수 없습니다: {file_path}")
        return

    print(f"📄 '{file_path}' 파일 테스트 시작...\n")

    # 실제 파일 열기
    with open(file_path, "rb") as f:
        # Django File 객체로 감싸서 전달
        django_file = File(f)

        try:
            extracted_text = extract_text_from_pdf(django_file)
            print("==========================================")
            print("✅ 텍스트 추출 성공!")
            print("==========================================")
            print(f"총 글자 수: {len(extracted_text)}자")
            print("\n--- [미리보기 (앞 300자)] ---")
            print(extracted_text[:300])
            print("==========================================")

        except PdfExtractionError as e:
            print("==========================================")
            print(f"❌ PDF 추출 예외 발생 (의도된 에러 처리):")
            print(f"  {e}")
            print("==========================================")
        except Exception as e:
            print("==========================================")
            print(f"💥 기타 예상치 못한 에러 발생:")
            print(f"  {e}")
            print("==========================================")


if __name__ == "__main__":
    # 🎯 여기에 테스트하고 싶은 실제 PDF 파일의 경로를 적어주세요!
    TEST_PDF_PATH = "sample.pdf"  # 예: "sample.pdf" 또는 "C:/Users/.../test.pdf"

    run_pdf_test(TEST_PDF_PATH)