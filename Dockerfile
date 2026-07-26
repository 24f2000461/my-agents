FROM python:3.12-slim

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

# Durable SQLite file lives on a mounted/persistent path in production;
# most free hosts (Render/Fly/Railway) give you a persistent disk you can
# mount at /data. Without one, data survives restarts but not redeploys.
ENV MAILROOM_DB_PATH=/data/mailroom.db
RUN mkdir -p /data

EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--timeout-keep-alive", "60"]
