FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/kotharikhushal/mcp-servers" \
      org.opencontainers.image.description="Health data ingest + MCP server (Pixel Watch / Health Connect)"
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
RUN useradd -u 1000 -m app && mkdir /data && chown app /data
USER app
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request as u;u.urlopen('http://127.0.0.1:8000/healthz',timeout=3)"
CMD ["python", "-m", "app.server"]
