# agentd/session-runner — §7.1
# Build: docker build -t agentd/session-runner:<version> docker/session-runner/
#   The context is this directory: COPY agentd_runner/ resolves relative to it,
#   so the -f form above failed with "/agentd_runner: not found".
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

# Role UIDs per §7.1 (setuid sweep runs last — after all install layers)
RUN groupadd -g 1001 uid_architect \
    && useradd -u 1001 -g 1001 -m -d /home/uid_architect -s /bin/bash uid_architect \
    && groupadd -g 1002 uid_developer \
    && useradd -u 1002 -g 1002 -m -d /home/uid_developer -s /bin/bash uid_developer

# gh CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/* \
    || echo "gh install skipped"

# Agent CLIs (§7.1 / #23) — Linux binaries, versions pinned (#19 exercised these).
# Install as root into a system prefix so role UIDs (1001/1002) share PATH.
ARG CLAUDE_CODE_VERSION=2.1.237
ARG GROK_CLI_VERSION=1.0.5
# Labels so tests can read pins from docker inspect (single source of truth).
LABEL agentd.claude_code_version="${CLAUDE_CODE_VERSION}" \
      agentd.grok_cli_version="${GROK_CLI_VERSION}"
RUN set -eux; \
    export HOME=/root; \
    curl -fsSL https://claude.ai/install.sh | bash -s "${CLAUDE_CODE_VERSION}"; \
    # Native installer lands under ~/.local; promote to /usr/local for all UIDs.
    install -d /usr/local/lib/claude /usr/local/bin; \
    if [ -L /root/.local/bin/claude ] || [ -f /root/.local/bin/claude ]; then \
      CLAUDE_REAL=$(readlink -f /root/.local/bin/claude); \
      install -m 0755 "${CLAUDE_REAL}" /usr/local/lib/claude/claude; \
      ln -sfn /usr/local/lib/claude/claude /usr/local/bin/claude; \
    else \
      echo "claude install failed: binary not found" >&2; exit 1; \
    fi; \
    curl -fsSL https://x.ai/cli/install.sh | bash -s "${GROK_CLI_VERSION}"; \
    if [ -L /root/.grok/bin/grok ] || [ -f /root/.grok/bin/grok ]; then \
      GROK_REAL=$(readlink -f /root/.grok/bin/grok); \
      install -m 0755 "${GROK_REAL}" /usr/local/bin/grok; \
    elif [ -f /root/.local/bin/grok ]; then \
      GROK_REAL=$(readlink -f /root/.local/bin/grok); \
      install -m 0755 "${GROK_REAL}" /usr/local/bin/grok; \
    else \
      echo "grok install failed: binary not found" >&2; exit 1; \
    fi; \
    # Drop installer caches (~400 MB second copy of the same binaries).
    rm -rf /root/.local /root/.claude /root/.grok; \
    # Pin assertions (literal match — dots are not regex wildcards)
    claude --version | tee /tmp/claude-ver.txt; \
    grep -Fq "${CLAUDE_CODE_VERSION}" /tmp/claude-ver.txt; \
    grok --version | tee /tmp/grok-ver.txt; \
    grep -Fq "${GROK_CLI_VERSION}" /tmp/grok-ver.txt; \
    # Role UIDs must resolve the same PATH entries (turns run after setuid)
    su -s /bin/bash uid_architect -c 'command -v claude && command -v grok && claude --version && grok --version'; \
    su -s /bin/bash uid_developer -c 'command -v claude && command -v grok && claude --version && grok --version'

# Root-owned dir for host-injected RPC bearer (docker create → docker cp → start).
RUN mkdir -p /etc/agentd && chmod 0755 /etc/agentd

WORKDIR /opt/agentd-runner
COPY agentd_runner/ /opt/agentd-runner/agentd_runner/
COPY entrypoint.sh /opt/agentd-runner/entrypoint.sh
RUN chmod 0755 /opt/agentd-runner/entrypoint.sh
ENV PYTHONPATH=/opt/agentd-runner
# Default PATH from debian includes sbin; keep it (no need to narrow for agents).
# 0.0.0.0 required for Docker port publish to the host; agents inside the
# container can also reach it — auth is the bearer on a root-only path.
ENV AGENTD_RPC_HOST=0.0.0.0
ENV AGENTD_RPC_PORT=7000
# Host mounts project tree at this path (§6.2 / #20).
ENV AGENTD_HOST_ROOT=/srv/agentd

# Final layer: strip setuid/setgid from the full image rootfs (§7.1 NO setuid).
# Must run after every package/CLI install so nothing reintroduces bits.
RUN find / -xdev \( -perm -4000 -o -perm -2000 \) -type f -print -delete 2>/dev/null || true \
    && ! find / -xdev \( -perm -4000 -o -perm -2000 \) -type f 2>/dev/null | grep -q . \
    && command -v claude && command -v grok \
    && claude --version && grok --version
# Runner is PID 1 as root; never runs agent tool code as root (§7.3)
EXPOSE 7000
ENTRYPOINT ["/opt/agentd-runner/entrypoint.sh"]
