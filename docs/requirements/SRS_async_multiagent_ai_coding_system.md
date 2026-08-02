# System Requirements Specification (SRS): Asynchronous Multi-Agent AI Coding System

**Document Version:** 1.3

**Changelog:** v1.3 — NFR-1.1 split into NFR-1.1a (unattended recovery from software faults) and NFR-1.1b (attended recovery from host boot). The host runs with FileVault enabled by owner policy, which makes unattended recovery from a cold boot impossible for any daemon; the requirement is amended rather than silently unmet. NFR-1.2 gains a clause covering deliveries missed during the resulting unbounded downtime window. Ruled by `@huozhe` on Issue #1; drafted by Claude Agent.

**Target Environment:** Local Mac mini (Apple Silicon M4 Pro, 24GB RAM) running OrbStack & GitHub Platform

**Purpose:** This document specifies the functional and non-functional requirements for an autonomous, asynchronous multi-agent AI coding system. It defines **what** the system must achieve and **under what operational constraints**, intentionally omitting implementation details to allow independent technical design proposals from each LLM.

---

## 1. System Vision & Target Environment

The system establishes an automated, asynchronous software development workflow using **GitHub as the central message bus and state machine**. Two AI agents—an **Architect/Planner** and a **Developer/Implementer**—collaborate asynchronously via GitHub Issues, Pull Requests (PRs), and code reviews to design, implement, and verify code changes with minimal human intervention.

### Target Environment Constraints

* **Primary Host:** Local Mac mini (M4 Pro chip, 24GB Unified Memory).
* **Container Runtime:** OrbStack (pre-installed and active on the host).
* **Storage & Resource Footprint:** The orchestrator and container configurations must operate comfortably within the 24GB RAM ceiling alongside host OS tasks.

---

## 2. Core Operational Constraints & Boundary Conditions

* **Configurable & Dynamic Agent Roles:** Roles are **not static**. The assignment of which LLM acts as the Architect/Planner vs. Developer/Implementer must be configurable per repository or per issue (e.g., Grok can act as Architect while Claude acts as Developer, or vice versa).
* **Role-Agnostic Account Identities:** GitHub account identities must be role-agnostic (e.g., `@claude-bot` and `@grok-bot`). The identity string used in communications and webhook filtering must match the account's registered GitHub username.
* **Scope Boundary:** Session context and execution lifecycle are bound to the **GitHub Issue level** (one session lifecycle per feature request/issue), supporting multiple PRs under a single issue before final teardown.
* **Zero Cold-Starts:** The agent execution runtime must avoid re-cloning repositories, re-ingesting full codebases, or losing conversation state between individual event interactions.

---

## 3. Functional Requirements

### 3.1 Identity, Configuration & Governance

* **FR-1.1 (Multi-Account Authentication):** The system shall authenticate agent actions on GitHub using two distinct user accounts (e.g., `@claude-bot` and `@grok-bot`).
* **FR-1.2 (Configurable Role Mapping):** The orchestrator shall allow dynamic configuration defining which bot holds the **Architect/Reviewer** role and which holds the **Developer/Implementer** role for a given issue or repository.
* **FR-1.3 (Branch Protection Compliance):** The system shall enforce distinct account credentials so that formal PR approvals from the designated Architect account satisfy GitHub branch protection rules for PRs opened by the Developer account.

### 3.2 Workflow & Collaboration Protocol

* **FR-2.1 (Issue Ingestion):** Upon creation of a new GitHub Issue, the orchestrator shall route the event to the designated Architect LLM to initiate architectural planning.
* **FR-2.2 (Design Phase):** The Architect LLM shall create an architectural spec (RFC) on a design branch and open a **Design PR**.
* **FR-2.3 (Peer Design Review):** The Developer LLM shall review the Design PR spec and either request changes or approve the design proposal.
* **FR-2.4 (Implementation Phase):** Upon design approval, the Developer LLM shall create a local workspace, implement code according to the approved RFC, and open a **Feature PR**.
* **FR-2.5 (Automated Code Review Loop):** The Architect LLM shall evaluate incoming diffs on Feature PRs against the approved RFC and post inline PR comments. The Developer LLM shall address comments, push fixes, and notify the Architect LLM.
* **FR-2.6 (PR Merge & Branch Deletion):** Upon receiving explicit approval from the Architect LLM and passing CI checks, **the Developer LLM shall merge the PR into the target branch and delete the feature branch**.

### 3.3 Human Verification, Escalation & Issue Gating

* **FR-3.1 (Human Verification Section Requirement):** Before code completion, the issue description (or main issue body) must include a dedicated **"Verification Protocol"** section added by the agents. This section must detail explicit, step-by-step instructions for the repository owner to test and verify that the issue has been fully addressed.
* **FR-3.2 (Human Verification Gate for Issue Closure):** Neither LLM agent shall close the issue automatically upon merging PRs. **Closing of the issue is strictly gated by the human owner.** The issue closing and cleanup workflow shall only begin after the human owner explicitly checks off the verification task (e.g., checking a Markdown checkbox `- [x] Human Verification Complete`) in the issue body.
* **FR-3.3 (Human Decision Escalation):** If an agent encounters unresolvable ambiguity, an architectural deadlock, or requires human authorization, either agent or the orchestrator gateway shall explicitly **tag the repository owner (`@username`) in a GitHub comment** to request input.
* **FR-3.4 (Escalation Pause):** Upon tagging the human owner for clarification, the active task execution for that issue shall enter a paused state until the human owner responds on GitHub.

### 3.4 Event Routing & Distributed Cleanup Responsibilities

* **FR-4.1 (Event Ingestion & Loop Prevention):** The gateway shall route webhooks (issues, PRs, comments, issue edits) to the corresponding issue session, filtering out bot self-messages to prevent recursive loops.
* **FR-4.2 (Gated Cleanup Trigger):** The teardown sequence shall be triggered **only when the issue is closed following human check-off verification**.
* **FR-4.3 (Agent-Level Cleanup Responsibilities):**
* **Developer LLM Cleanup:** During issue teardown, the Developer LLM / session runner shall clean up all local Git worktrees and development branches it created.
* **Architect LLM Cleanup:** The Architect LLM / session runner shall clean up any temporary evaluation files, diff artifacts, or scratchpads created during review.
* **Orchestrator Teardown:** The gateway orchestrator shall terminate issue-scoped containers and purge host-level session storage mounts.



---

## 4. Non-Functional & Operational Requirements

### 4.1 Availability & Auto-Recovery

* **NFR-1.1a (Unattended Recovery — Software Faults):** Following a crash of the orchestrator gateway, a session container, or the OrbStack runtime, the system shall recover automatically without human intervention.
* **NFR-1.1b (Attended Recovery — Host Boot):** The host runs with FileVault enabled by owner policy. Following a power outage or OS reboot, recovery therefore requires exactly one human action: unlocking the boot volume and logging in. After that action, OrbStack, the orchestrator gateway, and all active issue sessions shall resume automatically with no further human intervention. The host shall be configured to power on automatically when mains power is restored, so that the unlock is the only human action required.
* **NFR-1.2 (State Reconciliation):** Upon startup, the orchestrator shall query active GitHub PRs and issues, reconcile local routing state in SQLite, and re-inject context resume signals into active agent sessions. Because NFR-1.1b admits an unbounded downtime window, reconciliation shall additionally recover any webhook deliveries missed while the orchestrator was unavailable.

### 4.2 Resource Management & Storage Safeguards

* **NFR-2.1 (Memory & Disk Safeguards):** The system shall operate within the memory limits of the Mac mini M4 Pro (24GB RAM) and continuously monitor available host disk space.
* **NFR-2.2 (Storage Circuit Breaker):** If host disk space falls below 15 GB, the orchestrator shall pause incoming webhooks, halt new container creation, and send a macOS desktop notification to the host owner.

---

## 5. Out of Scope (For Independent Design Proposals)

The following implementation choices are **intentionally unconstrained** to allow Claude and Grok to propose their technical designs independently:

1. Specific database schema/engine for local gateway route tracking.
2. Programming language/framework for the local Webhook Gateway daemon.
3. Specific IPC streaming mechanisms (e.g., NDJSON `stdio` vs. JSON-RPC over `stdio`).
4. Container definition structure inside OrbStack.

