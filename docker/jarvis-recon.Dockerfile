# JARVIS recon toolkit — kali-mcp (has nmap/gobuster/ffuf/nikto/sqlmap/whatweb/subfinder/
# httpx/nuclei/amass/dnsx) plus baked-in nuclei templates and focused wordlists, so scans
# start instantly instead of re-downloading each run.
FROM kali-mcp:latest
RUN nuclei -update-templates 2>/dev/null || true
RUN mkdir -p /wl && \
    curl -fsSL -o /wl/dirs.txt  https://raw.githubusercontent.com/danielmiessler/SecLists/master/Discovery/Web-Content/raft-medium-directories.txt || true && \
    curl -fsSL -o /wl/subs.txt  https://raw.githubusercontent.com/danielmiessler/SecLists/master/Discovery/DNS/subdomains-top1million-5000.txt || true
