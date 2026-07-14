# JARVIS recon toolkit — kali-mcp (nmap/gobuster/ffuf/nikto/sqlmap/whatweb/subfinder/
# httpx/nuclei/amass/dnsx) + baked nuclei templates + wordlists + the Tier-1 URL-harvest,
# crawl, and vuln-pattern tools (gau/waybackurls/katana/gf/qsreplace/dalfox/hakrawler/subjs).
#
# The Go tools are built in the official golang image (Kali's apt mirror 403s in CI), then
# copied in — reliable, no apt on the kali base.

# ── stage 1: compile the Go toolkit ──────────────────────────────────────────────
FROM golang:1.25-bookworm AS gobuild
ENV GOBIN=/out GOFLAGS=-buildvcs=false CGO_ENABLED=0 GOTOOLCHAIN=auto
# Independent installs — a single flaky module can't abort the batch.
RUN mkdir -p /out; \
    go install github.com/lc/gau/v2/cmd/gau@latest || true; \
    go install github.com/tomnomnom/waybackurls@latest || true; \
    go install github.com/tomnomnom/qsreplace@latest || true; \
    go install github.com/tomnomnom/anew@latest || true; \
    go install github.com/tomnomnom/unfurl@latest || true; \
    go install github.com/tomnomnom/gf@latest || true; \
    go install github.com/projectdiscovery/katana/cmd/katana@latest || true; \
    go install github.com/hahwul/dalfox/v2@latest || true; \
    go install github.com/hakluke/hakrawler@latest || true; \
    ls -1 /out

# ── stage 2: the recon image ─────────────────────────────────────────────────────
FROM kali-mcp:latest
RUN nuclei -update-templates 2>/dev/null || true
RUN mkdir -p /wl && \
    curl -fsSL -o /wl/dirs.txt  https://raw.githubusercontent.com/danielmiessler/SecLists/master/Discovery/Web-Content/raft-medium-directories.txt || true && \
    curl -fsSL -o /wl/subs.txt  https://raw.githubusercontent.com/danielmiessler/SecLists/master/Discovery/DNS/subdomains-top1million-5000.txt || true

# Tier-1 Go tools (from stage 1)
COPY --from=gobuild /out/ /usr/local/bin/

# arjun (parameter discovery) — Python; the base has pip3.
RUN pip3 install --break-system-packages --quiet arjun 2>/dev/null || \
    pip3 install --quiet arjun 2>/dev/null || true

# gf pattern packs (LFI/XSS/SSRF/redirect/etc.) so `gf <tag>` works out of the box.
RUN mkdir -p /root/.gf && \
    (git clone --depth 1 https://github.com/tomnomnom/gf /tmp/gf 2>/dev/null && cp /tmp/gf/examples/*.json /root/.gf/ 2>/dev/null); \
    (git clone --depth 1 https://github.com/1ndianl33t/Gf-Patterns /tmp/gfp 2>/dev/null && cp /tmp/gfp/*.json /root/.gf/ 2>/dev/null); \
    rm -rf /tmp/gf /tmp/gfp || true
