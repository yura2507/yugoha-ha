FROM ghcr.io/home-assistant/base:latest

ARG BUILD_VERSION
ARG BUILD_ARCH

RUN apk add --no-cache python3 py3-pip \
    && python3 -m venv /opt/yugoha-venv

COPY requirements.txt /tmp/requirements.txt
RUN /opt/yugoha-venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt

COPY app.py /app/app.py
COPY bootstrap.py /app/bootstrap.py
COPY id_migration.py /app/id_migration.py
COPY integration /app/integration
COPY run.sh /run.sh

RUN chmod a+x /run.sh /app/bootstrap.py /app/id_migration.py

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}"

CMD ["/run.sh"]
