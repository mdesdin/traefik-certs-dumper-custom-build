# syntax=docker/dockerfile:1.4

# Pin by digest for reproducibility.
# Use: docker buildx imagetools inspect ghcr.io/ldez/traefik-certs-dumper:<VERSION>
# Replace <DIGEST> with the real one you get from the command above.
FROM ghcr.io/ldez/traefik-certs-dumper:v2.10@sha256:fcf105172378d14dc93d17169adf70bf75088625c2775daa5d4ce1ff42baf258

ARG TARGETPLATFORM
ARG STALWART_VERSION=v0.15.4

# Alpine/ash doesn't support "pipefail" like bash, but we can still:
# - stop on errors (-e)
# - error on unset vars (-u)
SHELL ["/bin/ash", "-eu", "-c"]

RUN apk add --no-cache \
      python3 \
      curl \
      postgresql-client \
    && update-ca-certificates

# Ensure directories exist
RUN mkdir -p /opt/bin /hooks

# -----------------------------
# Install stalwart-cli (pinned + multi-arch)
# -----------------------------
RUN set -eux; \
    case "${TARGETPLATFORM}" in \
      "linux/amd64") STALWART_TARGET="x86_64-unknown-linux-musl" ;; \
      "linux/arm64") STALWART_TARGET="aarch64-unknown-linux-musl" ;; \
      *) echo "Unsupported TARGETPLATFORM=${TARGETPLATFORM}" >&2; exit 1 ;; \
    esac; \
    ASSET="stalwart-cli-${STALWART_TARGET}.tar.gz"; \
    URL="https://github.com/stalwartlabs/stalwart/releases/download/${STALWART_VERSION}/${ASSET}"; \
    echo "Downloading stalwart-cli: ${URL}"; \
    curl -fsSL "${URL}" | tar xz -C /opt/bin; \
    chmod 0755 /opt/bin/stalwart-cli

# -----------------------------
# COPY hooks (reproducible)
# -----------------------------
COPY hooks/stalwart.py /hooks/stalwart.py
COPY hooks/postgres.sh /hooks/postgres.sh
RUN chmod 0755 /hooks/stalwart.py /hooks/postgres.sh

# -----------------------------
# Healthcheck
# - verifies stalwart-cli exists and runs
# - verifies your python hook compiles (syntax errors will fail build/run early)
# -----------------------------
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD /opt/bin/stalwart-cli --help >/dev/null 2>&1 \
      && python3 -m py_compile /hooks/stalwart.py
