FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gcc fonts-dejavu-core && rm -rf /var/lib/apt/lists/*

COPY requirements.txt* ./
RUN pip install --no-cache-dir -r requirements.txt 2>/dev/null || pip install --no-cache-dir fastapi uvicorn httpx curl_cffi Pillow

COPY . .

EXPOSE 5000

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "5000"]
