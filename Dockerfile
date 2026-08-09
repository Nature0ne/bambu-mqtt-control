FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apk add --no-cache ffmpeg \
    && addgroup -S -g 10001 bambu \
    && adduser -S -D -H -u 10001 -G bambu bambu \
    && mkdir -p /app /config /var/lib/bambu-control \
    && chown -R bambu:bambu /app /config /var/lib/bambu-control

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --requirement requirements.txt

COPY --chown=bambu:bambu app ./app
COPY --chown=bambu:bambu certs ./certs

USER 10001:10001
EXPOSE 9208

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9208", "--proxy-headers", "--forwarded-allow-ips", "*"]
