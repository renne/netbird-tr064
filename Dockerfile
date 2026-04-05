FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

VOLUME ["/config"]

ENV CONFIG_PATH=/config/config.yaml \
    LOG_LEVEL=INFO \
    PYTHONPATH=/app/src

CMD ["python", "-u", "src/main.py"]
