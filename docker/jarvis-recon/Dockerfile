# JARVIS recon/pentest toolkit — throwaway containers via `docker run --rm`.
# Build:  docker build -t jarvis-recon:latest -f docker/jarvis-recon/Dockerfile docker/jarvis-recon
#
# Uses GitHub release binaries (no `go install`) so builds finish in minutes and
# do not OOM on typical Docker Desktop memory limits.
FROM debian:bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive
ARG TARGETARCH=amd64

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash ca-certificates curl dnsutils nmap \
      python3 python3-pip git unzip wget \
 && (apt-get install -y --no-install-recommends sqlmap || true) \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /wl /usr/local/bin /root/.gf \
 && (curl -fsSL -o /wl/dirs.txt \
       https://raw.githubusercontent.com/danielmiessler/SecLists/master/Discovery/Web-Content/common.txt \
     || printf '%s\n' admin login api assets static uploads .git robots.txt > /wl/dirs.txt)

# Map Docker TARGETARCH → release asset arch names
RUN set -eux; \
  arch="${TARGETARCH}"; \
  case "$arch" in \
    amd64|x86_64) pd=amd64; ff=amd64 ;; \
    arm64|aarch64) pd=arm64; ff=arm64 ;; \
    *) echo "unsupported arch: $arch"; exit 1 ;; \
  esac; \
  tmp=/tmp/bins; mkdir -p "$tmp"; \
  # subfinder
  curl -fsSL -o "$tmp/sf.zip" \
    "https://github.com/projectdiscovery/subfinder/releases/download/v2.6.7/subfinder_2.6.7_linux_${pd}.zip"; \
  unzip -qo "$tmp/sf.zip" -d "$tmp/sf" && install -m 755 "$tmp/sf/subfinder" /usr/local/bin/subfinder; \
  # httpx
  curl -fsSL -o "$tmp/hx.zip" \
    "https://github.com/projectdiscovery/httpx/releases/download/v1.6.9/httpx_1.6.9_linux_${pd}.zip"; \
  unzip -qo "$tmp/hx.zip" -d "$tmp/hx" && install -m 755 "$tmp/hx/httpx" /usr/local/bin/httpx; \
  # nuclei
  curl -fsSL -o "$tmp/nu.zip" \
    "https://github.com/projectdiscovery/nuclei/releases/download/v3.3.7/nuclei_3.3.7_linux_${pd}.zip"; \
  unzip -qo "$tmp/nu.zip" -d "$tmp/nu" && install -m 755 "$tmp/nu/nuclei" /usr/local/bin/nuclei; \
  # ffuf
  curl -fsSL -o "$tmp/ff.tgz" \
    "https://github.com/ffuf/ffuf/releases/download/v2.1.0/ffuf_2.1.0_linux_${ff}.tar.gz"; \
  tar -xzf "$tmp/ff.tgz" -C "$tmp" && install -m 755 "$tmp/ffuf" /usr/local/bin/ffuf; \
  # waybackurls + gf (optional URL helpers)
  curl -fsSL -o /usr/local/bin/waybackurls \
    "https://github.com/tomnomnom/waybackurls/releases/download/v0.1.0/waybackurls-linux-${pd}-0.1.0" \
    || true; \
  chmod +x /usr/local/bin/waybackurls 2>/dev/null || true; \
  rm -rf "$tmp"; \
  subfinder -version; httpx -version; nuclei -version; ffuf -V

# Lightweight tech fingerprint when whatweb is absent (bookworm has no whatweb package).
RUN printf '%s\n' '#!/bin/bash' \
  'url="${1:-}"; [ -n "$url" ] || { echo "usage: whatweb <url>"; exit 1; }' \
  'curl -sSIL -m 12 -A "Mozilla/5.0 JARVIS-recon" "$url" 2>/dev/null | head -25' \
  > /usr/local/bin/whatweb && chmod +x /usr/local/bin/whatweb

# Minimal nikto stub message when real nikto is not installed (attack task still works via nmap/ffuf/nuclei/sqlmap).
RUN if ! command -v nikto >/dev/null 2>&1; then \
  printf '%s\n' '#!/bin/bash' \
    'echo "nikto is not in this image. Use nmap/nuclei/dirs/sqlmap tasks instead, or set JARVIS_KALI_IMAGE to a full Kali image."' \
    > /usr/local/bin/nikto && chmod +x /usr/local/bin/nikto; \
  fi

WORKDIR /work
ENTRYPOINT ["bash", "-lc"]
CMD ["echo jarvis-recon ready"]
