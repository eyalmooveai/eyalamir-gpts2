FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY find_bad_speed_limit.py app.py ./
COPY templates/ templates/

# Cloud Run injects PORT; gunicorn binds to it. One worker (the app keeps
# job/progress state in an in-memory dict shared across requests - see
# README.md's Cloud Run deployment section for why this means
# --max-instances=1 at deploy time too) with several threads, since a run
# started by one request continues in a background thread that later
# /status polls need to reach while it's still working.
ENV PORT=8080
EXPOSE 8080
CMD exec gunicorn --bind :$PORT --workers 1 --worker-class gthread --threads 8 --timeout 120 app:app
