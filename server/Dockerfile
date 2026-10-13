FROM python:3.12-slim

WORKDIR /srv
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# Data (sqlite DB, Apple session, secret) lives in a volume.
ENV DATA_DIR=/data
VOLUME ["/data"]

EXPOSE 8080
CMD ["uvicorn", "app.app:app", "--host", "0.0.0.0", "--port", "8080"]
