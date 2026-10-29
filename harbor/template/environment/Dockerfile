FROM node:22.23.2-bookworm-slim AS node
FROM python:3.11.15-slim-bookworm
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && npm install -g --ignore-scripts @earendil-works/pi-coding-agent@1.4.0 \
    && test "$(pi --version)" = 1.4.0

ARG DEBIAN_MIRROR=https://mirrors.ustc.edu.cn
RUN sed -i "s|http://deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && printf 'Acquire::http::Timeout "30";\nAcquire::https::Timeout "30";\nAcquire::Retries "2";\n' > /etc/apt/apt.conf.d/80-review-timeouts \
    && apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl git procps \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /bin/bash reviewer

# Only a fixed, scanned minimax snapshot, never .git, platform receipts or host homes.
COPY submission/ /submission/
COPY input/ /input/
COPY reviewer-skill/ /opt/reviewer-skill/
RUN chmod -R a-w /submission /input /opt/reviewer-skill \
    && mkdir -p /work/review /output/evidence \
    && chown -R reviewer:reviewer /work /output

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PI_TELEMETRY=0 \
    PI_SKIP_VERSION_CHECK=1
WORKDIR /work/review
USER reviewer
CMD ["sleep", "infinity"]
