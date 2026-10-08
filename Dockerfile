FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UPLOAD_DIR=/data/uploads PORT=8000
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY templates ./templates
RUN useradd -m app && mkdir -p /data/uploads && chown -R app:app /data /app
USER app
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/healthz')" || exit 1
CMD ["sh", "-c", "gunicorn -w 3 -k gthread --threads 4 -b 0.0.0.0:${PORT} --timeout 60 app:app"]
