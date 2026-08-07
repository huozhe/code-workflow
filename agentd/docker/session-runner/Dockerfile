# agentd/session-runner — §7.1
# Build: docker build -t agentd/session-runner:1.0.0 -f docker/session-runner/Dockerfile .
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        python3 \
        python3-pip \
        python3-venv \
        build-essential \
        ripgrep \
        fd-find \
    && rm -rf /var/lib/apt/lists/* \
    && git --version \
    # Role UIDs per §7.1
    && groupadd -g 1001 uid_architect \
    && useradd -u 1001 -g 1001 -m -d /home/uid_architect -s /bin/bash uid_architect \
    && groupadd -g 1002 uid_developer \
    && useradd -u 1002 -g 1002 -m -d /home/uid_developer -s /bin/bash uid_developer \
    # No setuid/setgid binaries on the image rootfs (M2 residual check)
    && find / -xdev \( -perm -4000 -o -perm -2000 \) -type f -print -delete 2>/dev/null || true

# gh CLI (optional for M2; install if available)
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/* \
    || echo "gh install skipped"

# Root-owned dir for host-injected RPC bearer (docker create → docker cp → start).
# Not on a bind mount — §5.2: credentials only on container-local FS / tmpfs.
RUN mkdir -p /etc/agentd && chmod 0755 /etc/agentd

WORKDIR /opt/agentd-runner
COPY agentd_runner/ /opt/agentd-runner/agentd_runner/
COPY entrypoint.sh /opt/agentd-runner/entrypoint.sh
RUN chmod 0755 /opt/agentd-runner/entrypoint.sh
ENV PYTHONPATH=/opt/agentd-runner
# 0.0.0.0 required for Docker port publish to the host; agents inside the
# container can also reach it — auth is the bearer on a root-only path.
ENV AGENTD_RPC_HOST=0.0.0.0
ENV AGENTD_RPC_PORT=7000

# Runner is PID 1 as root; never runs agent tool code as root (§7.3)
EXPOSE 7000
ENTRYPOINT ["/opt/agentd-runner/entrypoint.sh"]
