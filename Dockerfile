FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY lantern ./lantern
RUN mkdir -p /data && chown 10001:10001 /data
USER 10001:10001
EXPOSE 8080
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=4s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/readyz', timeout=3)"]
ENTRYPOINT ["python", "-m", "lantern"]
CMD ["--config", "/data/config.json", "--database", "/data/lantern.db", "--host", "0.0.0.0"]
