# syntax=docker/dockerfile:1
#
# Harbor prewarm image: the clean review base plus offline dependency caches and a
# pre-provisioned PowerContext service.
#
# Scope rules enforced by construction:
#   * The build context never contains contestant code. There is no COPY of
#     `submission/` or `input/`; the contestant snapshot is mounted at run time by
#     Harbor, exactly like harbor/template/environment/Dockerfile does.
#   * Every Python package is installed offline from the local wheel cache
#     (--no-index --find-links). No package index is contacted.
#   * The service runs as a non-root user; the PowerContext installation lives in its
#     own virtual environment so that the interpreters used by the reviewed
#     submission keep their own dependency versions.
#
# Inputs are fixed by hash: /opt/prewarm/inputs.json lists the sha256 of every file
# that produced this image and is re-verified inside the image at build time.

ARG PYTHON_IMAGE=python:3.11.15-slim-bookworm
ARG NODE_IMAGE=node:22.23.2-bookworm-slim

FROM ${NODE_IMAGE} AS node
FROM ${PYTHON_IMAGE} AS base

ARG DEBIAN_MIRROR=https://mirrors.ustc.edu.cn
ARG PI_VERSION=1.4.0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_INPUT=1 \
    PIP_CONFIG_FILE=/dev/null \
    PIP_RETRIES=1 \
    PIP_TIMEOUT=20 \
    PI_TELEMETRY=0 \
    PI_SKIP_VERSION_CHECK=1

# Runtime base: bounded apt mirrors, Node 22 copied from the pinned node image and
# the pinned Pi release. No contestant code is copied or executed here.
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

# ---------------------------------------------------------------------------
# PowerContext is built and installed into an isolated virtual environment.
# The source is the exported, hash-pinned snapshot under /pc-source; the version
# is faked on purpose because the exported tree carries no Git metadata.
# ---------------------------------------------------------------------------
FROM base AS pc-build

ARG PC_VERSION=1.2.0
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${PC_VERSION}

COPY requirements.pc.txt /requirements.pc.txt
COPY wheels/pc /wheels/pc
COPY pc-source /pc-source
COPY scripts /scripts
COPY inputs.json /inputs.json

RUN set -eux; \
    python3 /scripts/verify_inputs.py --manifest /inputs.json \
        --check pc-source=/pc-source \
        --check requirements.pc.txt=/requirements.pc.txt \
        --check wheels/pc=/wheels/pc \
        --check scripts=/scripts; \
    python3 -m venv /opt/pc-venv; \
    /opt/pc-venv/bin/python -m pip install --no-index --no-cache-dir --disable-pip-version-check \
        --find-links /wheels/pc -r /requirements.pc.txt; \
    /opt/pc-venv/bin/python -m pip install --no-index --no-cache-dir --disable-pip-version-check \
        --find-links /wheels/pc hatchling hatch-vcs setuptools-scm; \
    /opt/pc-venv/bin/python -m pip install --no-index --no-cache-dir --disable-pip-version-check \
        --find-links /wheels/pc --no-build-isolation /pc-source; \
    /opt/pc-venv/bin/python -c "import importlib.metadata as m; assert m.version('powercontext') == '${PC_VERSION}', m.version('powercontext')"; \
    /opt/pc-venv/bin/powercontext server run --help > /dev/null; \
    rm -rf /wheels /pc-source /scripts

# ---------------------------------------------------------------------------
# Runtime image: the review base plus the PowerContext venv, the seekdb/pymysql
# wheel cache for explicit agent installs, and the service bootstrap scripts.
# ---------------------------------------------------------------------------
FROM base AS runtime

COPY --from=pc-build /opt/pc-venv /opt/pc-venv
COPY wheels/seekdb /opt/wheels/seekdb
COPY scripts /opt/prewarm
COPY inputs.json /opt/prewarm/inputs.json
COPY Dockerfile /opt/prewarm/Dockerfile
COPY scripts/bootstrap_services.py /opt/review/bootstrap.py
COPY scripts/pi-with-services.py /opt/review/pi-with-services.py

RUN set -eux; \
    python3 /opt/prewarm/verify_inputs.py --manifest /opt/prewarm/inputs.json \
        --check scripts=/opt/prewarm \
        --check wheels/seekdb=/opt/wheels/seekdb \
        --check Dockerfile=/opt/prewarm/Dockerfile; \
    chmod -R a+rX /opt/pc-venv /opt/wheels /opt/prewarm /opt/review; \
    chmod 0755 /opt/review/bootstrap.py /opt/review/pi-with-services.py /opt/review/bin/pi; \
    mkdir -p /work/review /output/evidence; \
    id -u reviewer >/dev/null 2>&1 || useradd --create-home --uid 10001 --shell /bin/bash reviewer; \
    chown -R reviewer:reviewer /work /output /opt/prewarm

# The PowerContext virtual environment is deliberately NOT on PATH: the reviewed
# submission keeps the interpreter and dependency versions it was written against.
# Use /opt/pc-venv/bin/powercontext (or activate the venv) explicitly.

WORKDIR /work/review
USER reviewer
CMD ["sleep", "infinity"]
