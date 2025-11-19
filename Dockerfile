FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends build-essential gcc libpq-dev && apt-get clean && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . /app
RUN python -m pip install --upgrade pip
RUN pip install flask twilio gunicorn
ENV PORT=5000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD curl -f http://localhost:${PORT}/health || exit 1
CMD exec gunicorn app:app --bind 0.0.0.0:${PORT} --workers 3 --threads 2
