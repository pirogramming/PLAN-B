FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# PDF OCR용 tesseract (한국어 팩 포함) — pdf_extractor.py가 pytesseract로 호출함
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-kor \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# collectstatic은 SECRET_KEY/DATABASE_URL 등 env가 필요한데 빌드 타임엔 없으므로 더미값으로 실행
RUN SECRET_KEY=build-time-dummy-key \
    DATABASE_URL=sqlite:///build.sqlite3 \
    python manage.py collectstatic --noinput

EXPOSE 8080

CMD exec gunicorn config.wsgi:application \
    --bind 0.0.0.0:8080 \
    --workers 2 \
    --threads 4 \
    --timeout 300