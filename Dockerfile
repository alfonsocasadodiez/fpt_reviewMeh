FROM python:3.12-slim

# ffmpeg for proxies and frames; pango for PDF reports; DejaVu for captions on frames
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[pdf]"

RUN useradd --create-home reviewmeh && mkdir /data && chown reviewmeh /data
USER reviewmeh
ENV REVIEWMEH_DATA_DIR=/data REVIEWMEH_HOST=0.0.0.0
VOLUME /data
EXPOSE 8086
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:' + __import__('os').getenv('REVIEWMEH_PORT', '8086') + '/health', timeout=4)"
CMD ["reviewmeh", "serve"]
