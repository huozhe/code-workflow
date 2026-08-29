# ADR-2: SQLite as a Derived Cache

*A decision of record for `agentd`. Index and context: [unified design spec §16](../unified_design_spec.md#16-architecture-decision-records).*


*Resolves SRS §5.1.* SQLite in WAL mode, single writer, treated as **cache, queue, and idempotency ledger** — not a system of record.

The stance matters more than the engine, and it was unanimous across all three Phase 1 drafts. Because GitHub holds truth (P1), the schema stays small, migrations can be destructive in the worst case, and recovery is a re-read rather than a repair. Postgres would add a second daemon to keep alive across reboots for no benefit; flat files would lose the atomicity that the idempotency ledger genuinely depends on.
