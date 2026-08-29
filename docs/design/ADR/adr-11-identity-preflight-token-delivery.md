# ADR-11: Identity Preflight on Token Delivery

*A decision of record for `agentd`. Index and context: [unified design spec §16](../unified_design_spec.md#16-architecture-decision-records).*


*Added during M0 dry-run after a wrong-account `gh` post attributed agent work to the owner.* §5.2 prevents cross-agent token confusion *inside* the container but not operator mis-mapping of PATs in Keychain/config.

**Rule:** before any turn after `session.init` / `session.resume`, assert `GET /user` login matches the configured identity for each role token. Fail closed + escalate on mismatch.

**Complement:** gateway escalates on `sender.login == owner` combined with an `agentd:turn` provenance footer.

*Rejected: trust config forever after first successful boot.* Silent wrong-token operation is indistinguishable from legitimate human action at the verification and loop-budget gates.
