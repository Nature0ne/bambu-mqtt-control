FROM python:3.12-alpine@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df

ARG VERSION=dev
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown

LABEL org.opencontainers.image.title="Bambu MQTT Control" \
      org.opencontainers.image.description="Local web dashboard for Bambu Lab printers and AMS units" \
      org.opencontainers.image.url="https://github.com/Nature0ne/bambu-mqtt-control" \
      org.opencontainers.image.source="https://github.com/Nature0ne/bambu-mqtt-control" \
      org.opencontainers.image.documentation="https://github.com/Nature0ne/bambu-mqtt-control#readme" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BAMBU_CONTROL_VERSION=${VERSION}

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
COPY --chown=bambu:bambu LICENSE THIRD_PARTY_NOTICES.md /licenses/

USER 10001:10001
EXPOSE 9208

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9208", "--proxy-headers", "--forwarded-allow-ips", "*"]
