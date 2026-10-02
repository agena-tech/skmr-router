#!/bin/bash
set -euo pipefail
# Messaging may host independently of a local SMB mount. Missing storage must
# still fail validation instead of being interpreted as an empty vault.
systemctl start agentcomm
python3 /root/agentcomm/send.py health
read -r storage_host vault < <(python3 -c 'import json; c=json.load(open("/root/agentcomm/agent.conf")); print(int(c.get("storage_host", True)), c["vault_local"])')
if [[ "$storage_host" == 1 ]]; then
    command -v mount.cifs >/dev/null || { echo 'cifs-utils is required for canonical storage.' >&2; exit 1; }
    mkdir -p "$vault"
    mountpoint -q "$vault" || mount "$vault"
    mountpoint -q "$vault" || { echo 'Canonical SMB mount unavailable.' >&2; exit 1; }
fi
python3 /root/.claude/skmr/cli.py obsidian-memory validate --no-plan
