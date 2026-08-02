# Technical Design Specification: Asynchronous Multi-Agent AI Coding System

**Author:** Gemini Agent  
**Version:** 1.0  
**Target Host:** Mac mini (Apple Silicon M4 Pro, 24GB RAM) with OrbStack  
**SRS Reference:** `plans/design/SRS_async_multiagent_ai_coding_system.md` (v1.2)

---

## 1. System Architecture Overview

The system operates as an event-driven, issue-scoped multi-agent orchestration platform. A lightweight Python-based **Webhook Gateway & Orchestrator Daemon** runs on the host (or inside a base OrbStack container), receiving webhooks from GitHub and routing events to persistent, issue-dedicated agent containers.

```
+-----------------------------------------------------------------------------------+
| GitHub Platform (Issues, PRs, Webhooks, Branch Protection)                         |
+-----------------------------------------------------------------------------------+
                                   | (Webhooks)               ^ (REST / GraphQL API)
                                   v                          |
+-----------------------------------------------------------------------------------+
| HOST (Mac mini M4 Pro, 24GB RAM)                                                  |
|                                                                                   |
|  +-----------------------------------------------------------------------------+  |
|  | Webhook Gateway & Orchestrator Daemon (FastAPI + SQLite + Event Router)      |  |
|  +-----------------------------------------------------------------------------+  |
|                                   | (IPC / Container Exec Streams)                |
|                                   v                                               |
|  +-----------------------------------------------------------------------------+  |
|  | OrbStack Runtime                                                            |  |
|  |                                                                             |  |
|  |  +--------------------------------+   +----------------------------------+  |  |
|  |  | Issue Container (#123)         |   | Issue Container (#124)           |  |  |
|  |  |  - Architect Agent Instance    |   |  - Architect Agent Instance      |  |  |
|  |  |  - Developer Agent Instance    |   |  - Developer Agent Instance      |  |  |
|  |  |  - Isolated Git Worktrees      |   |  - Isolated Git Worktrees        |  |  |
|  |  +--------------------------------+   +----------------------------------+  |  |
|  +-----------------------------------------------------------------------------+  |
+-----------------------------------------------------------------------------------+
```

---

## 2. Component Subsystems

### 2.1 Webhook Gateway & Event Router
* **Implementation:** Python 3.12 (FastAPI + Uvicorn) backed by SQLite (`gateway_state.db`).
* **Identity & Loop Prevention:** Every incoming webhook is parsed for `sender.login`. If `sender.login` matches any configured bot identity (`@claude-bot`, `@grok-bot`, `@gemini-bot`), the payload is dropped immediately (`HTTP 200 OK - Ignored self-event`).
* **At-Most-Once Delivery:** Deduplication table indexes `x-github-delivery` header values. Duplicate webhook IDs are discarded.

### 2.2 Issue Session & Container Orchestration
* **Lifecycle Binding:** Session life cycle is strictly bound to `issue.number`.
* **Zero Cold-Starts:** Upon receiving `issues.opened`, the Orchestrator spawns an OrbStack container (`issue-<id>`). Inside this container, persistent background processes (`stdio` or long-lived agent CLI sessions) are initialized and kept warm.
* **Stream Forwarding:** Incoming webhooks for `issue.number` (comments, PR reviews, edits) are formatted as structured context prompts and piped directly to the target agent process's stdin/socket without restarting container processes.

### 2.3 Dynamic Role Mapper
A repository configuration file (`.github/agent-roles.yaml`) defines role assignments per repository or issue:

```yaml
roles:
  architect: "claude-bot"
  developer: "grok-bot"
```

The Orchestrator dynamically binds bot identities to execution loops based on this configuration, enabling complete role swapability (e.g., Grok as Architect, Claude as Developer).

---

## 3. Workflow Protocol & Human Verification Gate

1. **Architect Planning (RFC):** Architect agent creates branch `design/<issue-id>`, writes the design spec, and opens a Design PR.
2. **Developer Design Review:** Developer agent reviews the Design PR and submits approval.
3. **Feature Implementation:** Developer agent creates a local Git worktree (`worktree-dev-#<issue-id>`), implements changes, and opens a Feature PR.
4. **Automated Review Loop:** Architect agent reviews diffs and posts inline comments. Developer agent pushes fixes until Architect grants explicit PR approval.
5. **PR Merge & Branch Cleanup:** Upon approval and CI green light, the **Developer agent** merges the PR into `main` and deletes the remote feature branch.
6. **Human Verification Protocol (Issue Closing Gate):**
   * Before finalizing implementation, agents insert a `## Verification Protocol` section into the primary GitHub Issue description containing a step-by-step checklist.
   * Neither agent closes the issue automatically.
   * The human repo owner tests the changes and checks off `- [x] Human Verification Complete`.
   * An `issues.edited` or `issue_comment` event containing the completed check triggers the final cleanup sequence.

---

## 4. Distributed Resource Cleanup Protocol

Upon detecting issue closure following human verification:
1. **Developer Agent Cleanup:** Executes `git worktree remove` for all development worktrees associated with the issue and deletes stale local tracking branches.
2. **Architect Agent Cleanup:** Purges temporary evaluation files, diff cache, and local scratchpad artifacts.
3. **Orchestrator Teardown:** Terminate issue container (`orb container stop issue-<id>`), unmount local volumes, and update SQLite state to `CLOSED`.

---

## 5. Non-Functional & Resilience Mechanisms

### 5.1 Resource Management & Disk Circuit Breaker
* **Host Resource Allocation:** OrbStack limits container memory total to 14GB, leaving 10GB RAM dedicated to macOS and host overhead.
* **Disk Guard Daemon:** A background thread monitors available host disk space every 30 seconds (`shutil.disk_usage`).
* **Threshold Trigger (< 15 GB):**
  1. Gateway transitions status to `PAUSED`.
  2. Incoming webhooks return `HTTP 503` (GitHub retries automatically).
  3. Dispatches desktop alert via macOS `osascript -e 'display notification "Disk space below 15GB. System paused." title "Orchestrator Alert"'`.

### 5.2 Auto-Recovery & Reconciliation
* **Host Startup:** Managed via macOS `launchd` plist pointing to the Orchestrator daemon and OrbStack.
* **State Sync:** On startup, the Orchestrator queries GitHub GraphQL API for open issues and open PRs, reconciles SQLite state table, re-establishes container handles, and injects context resume signals into active agent streams.
