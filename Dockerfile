FROM python:3.12-slim

WORKDIR /app

# pandas/pyarrow 등 일부 패키지의 소스 빌드에 필요한 최소 도구 + healthcheck용 curl
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD curl --fail http://localhost:${PORT:-8501}/_stcore/health || exit 1

# Render 등 일부 플랫폼은 PORT 환경변수로 리스닝 포트를 지정하므로,
# 없으면 기본값 8501을 사용한다.
CMD streamlit run app.py \
    --server.port=${PORT:-8501} \
    --server.address=0.0.0.0 \
    --server.headless=true
