FROM python:3.11-slim

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

WORKDIR /app
COPY zimi/ ./zimi/

RUN useradd -m -u 1000 zimi && chown -R zimi:zimi /app
USER zimi

ENV ZIM_DIR=/zims
ENV ZIMI_DATA_DIR=/zims/.zimi
ENV ZIMI_MANAGE=1
EXPOSE 8899

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8899/health')"

CMD ["python3", "-m", "zimi", "serve", "--port", "8899"]
