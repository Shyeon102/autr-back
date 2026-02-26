FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md /app/
COPY src /app/src
COPY scripts /app/scripts
COPY docker /app/docker

RUN pip install --no-cache-dir -e .

EXPOSE 8000
CMD ["uvicorn", "autr.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
