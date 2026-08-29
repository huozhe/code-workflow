# ADR-10: Accept-and-Queue Ingress

*A decision of record for `agentd`. Index and context: [unified design spec §16](../unified_design_spec.md#16-architecture-decision-records).*


Ingress never returns 5xx for a downstream condition. GitHub does not automatically retry failed repository webhook deliveries, so a rejected delivery is a lost delivery. Persisting first and deciding later costs one `INSERT` and removes an entire class of silent data loss — including during the circuit-breaker pause NFR-2.2 mandates.

Two of the three Phase 1 drafts specified 503-on-breaker, one of them justified by the incorrect belief that GitHub retries automatically. Both conceded.

**M0 hardening:** the webhook HMAC secret is loaded **once at process start** from Keychain (or a documented test-only env override). If unavailable, the process **refuses to bind a port**. Returning 5xx when the secret is missing would permanently drop deliveries during the NFR-1.1b post-unlock window when the login keychain may lag LaunchAgent start.
