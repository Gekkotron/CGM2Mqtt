FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY cgm2mqtt.py .

RUN useradd --system --no-create-home --uid 10001 cgm2mqtt
USER cgm2mqtt

CMD ["python", "cgm2mqtt.py"]
