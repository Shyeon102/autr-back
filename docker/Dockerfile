FROM python:3.12-slim

RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 1) 의존성 레이어 — pyproject.toml 안 바뀌면 캐시 히트
COPY pyproject.toml README.md /app/
RUN mkdir -p /app/src/autr && echo '' > /app/src/autr/__init__.py \
    && pip install --no-cache-dir -e .

# 2) 소스 레이어 — 코드만 바뀌면 여기서부터만 재빌드
#    editable install이라 src/ 덮어쓰면 바로 반영됨
COPY src /app/src
COPY scripts /app/scripts
COPY docker /app/docker
COPY alembic.ini /app/

EXPOSE 8000
CMD ["uvicorn", "autr.api.app:app", "--host", "0.0.0.0", "--port", "8000"]