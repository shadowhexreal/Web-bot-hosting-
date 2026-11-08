# SHADOW HOSTING - Railway / Docker image
# Includes: Python 3.12, Node.js (official build, checksum verified), cloudflared, git
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NODE_BIN=/usr/local/bin/node \
    CLOUDFLARED_BIN=/usr/local/bin/cloudflared \
    CLOUDFLARED_PROTOCOL=http2 \
    RECOVER_ON_BOOT=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl xz-utils git tini \
 && rm -rf /var/lib/apt/lists/*

# Node.js (latest release of the chosen major, SHA-256 verified against nodejs.org)
ARG NODE_MAJOR=22
RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
      amd64) NARCH=x64 ;; \
      arm64) NARCH=arm64 ;; \
      *) echo "Unsupported CPU architecture"; exit 1 ;; \
    esac; \
    base="https://nodejs.org/dist/latest-v${NODE_MAJOR}.x"; \
    curl -fsSL "$base/SHASUMS256.txt" -o /tmp/SHASUMS256.txt; \
    file="$(grep -o "node-v[0-9.]*-linux-${NARCH}\.tar\.xz" /tmp/SHASUMS256.txt | head -n1)"; \
    test -n "$file"; \
    curl -fsSLO "$base/$file"; \
    grep " ${file}\$" /tmp/SHASUMS256.txt | sha256sum -c -; \
    tar -xJf "$file" -C /usr/local --strip-components=1 --no-same-owner; \
    rm -f "$file" /tmp/SHASUMS256.txt; \
    node --version; npm --version

# Cloudflare tunnel client (used for the static website links)
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    curl -fsSL -o /usr/local/bin/cloudflared "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${arch}"; \
    chmod +x /usr/local/bin/cloudflared; \
    cloudflared --version

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY . .
RUN sed -i 's/\r$//' /app/start.sh && chmod +x /app/start.sh

EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/app/start.sh"]
