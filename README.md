# code-workflow

Design work for an asynchronous multi-agent AI coding system that uses **GitHub as the central message bus and state machine**.

Two AI agents — an **Architect/Planner** and a **Developer/Implementer** — collaborate through GitHub Issues, Pull Requests, and code reviews to design, implement, and verify changes, with issue closure gated on explicit human verification.

Target environment: a local Mac mini (M4 Pro, 24GB RAM) running OrbStack.

## Contents

- [`plans/design/SRS_async_multiagent_ai_coding_system.md`](plans/design/SRS_async_multiagent_ai_coding_system.md) — System Requirements Specification (v1.2). Specifies *what* the system must do and under what constraints; implementation choices are intentionally left open for independent design proposals.

## Status

Specification stage. No implementation yet.
