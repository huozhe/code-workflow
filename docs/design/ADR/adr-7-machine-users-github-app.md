# ADR-7: Machine Users, Not a GitHub App

*A decision of record for `agentd`. Index and context: [unified design spec §16](../unified_design_spec.md#16-architecture-decision-records).*


SRS §2 requires identity strings matching registered GitHub usernames for communications and webhook filtering; App bots surface as `app-name[bot]` and their reviews interact with branch protection through a different mechanism. Two machine users match the SRS literally and keep the model simple: each agent *is* a GitHub user.

*Cost accepted:* manual PAT rotation; per-account rate limits (5,000 req/h each, far above expected load). If the system later spans many repositories or organizations, an App becomes the better answer and this ADR should be revisited.
