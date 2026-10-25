FROM python:3.12-slim

WORKDIR /app

# 기존 Streamlit 분석 도구와 모바일 웹의 공통 실행 환경.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD curl --fail http://localhost:${PORT:-8501}/health || exit 1

# Render 등 일부 플랫폼은 PORT 환경변수로 리스닝 포트를 지정하므로,
# 없으면 기본값 8501을 사용한다.
CMD python -m gunicorn --bind 0.0.0.0:${PORT:-8501} \
    --workers 1 --threads 4 --timeout 60 webapp:app
