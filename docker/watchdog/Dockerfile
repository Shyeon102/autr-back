FROM python:3.12-slim
WORKDIR /app
COPY docker/watchdog/watchdog.py /app/watchdog.py
RUN pip install --no-cache-dir requests
CMD ["python", "/app/watchdog.py"]
