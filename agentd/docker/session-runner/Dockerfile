# agentd/session-runner — §7.1
# Build: docker build -t agentd/session-runner:1.0.0 -f docker/session-runner/Dockerfile .
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

# Build deps + runtime tools (bookworm git is 2.39 — too old for §6.4 pin)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        xz-utils \
        python3 \
        python3-pip \
        python3-venv \
        build-essential \
        gettext \
        libcurl4-openssl-dev \
        libexpat1-dev \
        libssl-dev \
        zlib1g-dev \
        ripgrep \
        fd-find \
    && rm -rf /var/lib/apt/lists/*

# git ≥ 2.48 (§6.4 worktree.useRelativePaths / §7.1 pin) — build from release tarball
ARG GIT_VERSION=2.48.1
RUN curl -fsSL "https://www.kernel.org/pub/software/scm/git/git-${GIT_VERSION}.tar.xz" \
      -o /tmp/git.tar.xz \
    && tar -C /tmp -xJf /tmp/git.tar.xz \
    && cd "/tmp/git-${GIT_VERSION}" \
    && make -j"$(nproc)" prefix=/usr/local NO_TCLTK=1 NO_GETTEXT=1 all \
    && make prefix=/usr/local NO_TCLTK=1 NO_GETTEXT=1 install \
    && cd / && rm -rf "/tmp/git-${GIT_VERSION}" /tmp/git.tar.xz \
    && git --version \
    && git --version | grep -E 'git version 2\.(4[8-9]|[5-9][0-9])|git version [3-9]'

# Node.js LTS (§7.1) via NodeSource
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && node --version && npm --version

# Role UIDs per §7.1; strip setuid/setgid from the image rootfs
RUN groupadd -g 1001 uid_architect \
    && useradd -u 1001 -g 1001 -m -d /home/uid_architect -s /bin/bash uid_architect \
    && groupadd -g 1002 uid_developer \
    && useradd -u 1002 -g 1002 -m -d /home/uid_developer -s /bin/bash uid_developer \
    && find / -xdev \( -perm -4000 -o -perm -2000 \) -type f -print -delete 2>/dev/null || true

# gh CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/* \
    || echo "gh install skipped"

# Root-owned dir for host-injected RPC bearer (docker create → docker cp → start).
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
# Host mounts ~/.agentd at this path so repos/ and sessions/ keep topology (§6.4).
ENV AGENTD_HOST_ROOT=/srv/agentd

# Runner is PID 1 as root; never runs agent tool code as root (§7.3)
EXPOSE 7000
ENTRYPOINT ["/opt/agentd-runner/entrypoint.sh"]
