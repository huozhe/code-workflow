#!/bin/bash
# Normalize host-injected bearer onto real container root ownership before bind.
# OrbStack's docker cp may leave the file owned by a host-mapped UID; without
# CAP_DAC_OVERRIDE, even container root cannot read mode 0400 of another UID.
set -euo pipefail
BEARER=/etc/agentd/rpc.bearer
if [[ -f "$BEARER" ]]; then
  chown root:root "$BEARER"
  chmod 0400 "$BEARER"
fi
exec python3 -m agentd_runner
