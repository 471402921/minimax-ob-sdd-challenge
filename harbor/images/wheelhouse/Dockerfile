# syntax=docker/dockerfile:1
#
# Player-matching prewarm image. The trial-local PowerContext service and the
# submission's own wheel cache come from the SAME resolved dependency set, so a
# version skew between service and client cannot be introduced by the image.
# No contestant code is copied at build time.
ARG PYTHON_IMAGE=python:3.11.15-slim-bookworm
ARG NODE_IMAGE=node:22.23.2-bookworm-slim

FROM ${NODE_IMAGE} AS node

FROM ${PYTHON_IMAGE}

ARG DEBIAN_MIRROR=https://mirrors.ustc.edu.cn
ARG PI_VERSION=1.4.0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_INPUT=1 \
    PIP_CONFIG_FILE=/dev/null \
    PIP_TIMEOUT=20 \
    PIP_RETRIES=1 \
    PI_TELEMETRY=0 \
    PI_SKIP_VERSION_CHECK=1 \
    PIP_FIND_LINKS=/opt/wheels/player

RUN set -eux; \
    sed -i "s|http://deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    printf 'Acquire::http::Timeout "30";\nAcquire::https::Timeout "30";\nAcquire::Retries "2";\n' > /etc/apt/apt.conf.d/80-review-timeouts; \
    apt-get update; \
    apt-get install -y --no-install-recommends bash ca-certificates curl git procps; \
    rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
COPY scripts /scripts
RUN set -eux; \
    ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm; \
    ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx; \
    npm install -g --ignore-scripts "@earendil-works/pi-coding-agent@${PI_VERSION}"; \
    test "$(pi --version)" = "${PI_VERSION}"; \
    npm cache clean --force; \
    mkdir -p /opt/review/bin; \
    REAL_PI="$(readlink -f "$(command -v pi)")"; \
    test -n "${REAL_PI}"; \
    ln -sf "${REAL_PI}" /opt/review/pi-original; \
    ln -sf "${REAL_PI}" /opt/review/bin/pi-original; \
    install -m 0755 /scripts/pi-with-services.py /usr/local/bin/pi; \
    install -m 0755 /scripts/pi-with-services.py /opt/review/bin/pi; \
    install -m 0755 /scripts/pi-with-services.py /opt/review/pi-with-services.py; \
    rm -rf /scripts

# One offline wheel set serves BOTH /opt/pc-venv and the submission's .venv.
COPY wheels/player /opt/wheels/player
COPY scripts /opt/prewarm
COPY inputs.json /opt/prewarm/inputs.json
COPY Dockerfile /opt/prewarm/Dockerfile

RUN set -eux; \
    python3 /opt/prewarm/verify_inputs.py --manifest /opt/prewarm/inputs.json \
        --check wheels/player=/opt/wheels/player --check scripts=/opt/prewarm; \
    python3 -m venv /opt/pc-venv; \
    /opt/pc-venv/bin/python -m pip install --no-index --no-cache-dir \
        --find-links /opt/wheels/player -r /opt/wheels/player/requirements.service.txt; \
    /opt/pc-venv/bin/python -c "import importlib.metadata as m; assert m.version('powercontext').split('.')[:2] == ['1', '4'], m.version('powercontext')"; \
    /opt/pc-venv/bin/powercontext server run --help > /dev/null; \
    cp /opt/prewarm/bootstrap_services.py /opt/review/bootstrap.py; \
    cp /opt/prewarm/pi-with-services.py /opt/review/pi-with-services.py; \
    chmod 0755 /opt/review/bootstrap.py /opt/review/pi-with-services.py; \
    chmod -R a+rX /opt/pc-venv /opt/wheels /opt/prewarm /opt/review; \
    mkdir -p /work/review /output/evidence; \
    id -u reviewer >/dev/null 2>&1 || useradd --create-home --uid 10001 --shell /bin/bash reviewer; \
    chown -R reviewer:reviewer /work /output

# The index is deliberately NOT disabled: unlisted packages still resolve, and
# the runtime guard bounds that attempt. Nothing here may read host credentials.

WORKDIR /work/review
USER reviewer
CMD ["sleep", "infinity"]
