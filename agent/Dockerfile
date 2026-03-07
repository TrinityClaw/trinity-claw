FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    docker.io curl \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-ita \
    tesseract-ocr-spa \
    tesseract-ocr-ell \
    tesseract-ocr-fra \
    tesseract-ocr-srp \
    tesseract-ocr-srp-latn

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    playwright install chromium --with-deps

COPY . .

CMD ["python", "app.py"]
