FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    INTERWAL_SEKUND=60 \
    OKNO_GODZIN=2
WORKDIR /app
COPY alerty.py .
CMD ["python", "alerty.py"]
