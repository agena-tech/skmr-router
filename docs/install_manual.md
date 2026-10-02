# SKMR — Manual Installation

[Project overview](../README.md) · [Full auto installation](install_full_auto.md)

**Method B installs SKMR into the current Linux environment.** Use it when Claude runs in your main Linux terminal and its peer runs on another machine, or when you need to manage networking and deployment yourself. The installer can provision dependencies and write configuration, but you supply the addresses, identities, grants, credential transfer, and remote mounts.

This guide builds a concrete pair: **`atlas` is a Commander with WRITE access**, and **`nova` is a Lieutenant with READ access** to Atlas's vault. Nova investigates and sends recommendations; Atlas owns permanent-memory commits.

## 1. Prepare both machines

Use Linux, Python 3.11 or newer, internet access, and the complete SKMR package on both machines. Windows users enter Linux with `wsl -d Ubuntu` and use Linux paths.

The examples install under **root** on both hosts. This matches the packaged command paths and default service unit:

```bash
sudo -i
cd /path/to/skmr
python3 --version
```

Replace `/path/to/skmr` with the actual project directory. Keep `install.py`, `payload/`, and `knowledge/` together. Run installation and Claude under the same account; otherwise Claude may load another account's configuration.

### Install Claude Code separately

Method B installs Claude integration files; it **does not download the Claude Code CLI**. Install the native CLI on each host, following the [official setup guide](https://code.claude.com/docs/en/setup):

```bash
curl -fsSL https://claude.ai/install.sh | bash
export PATH="$HOME/.local/bin:$PATH"
claude --version
claude auth login
```

Use `bash -l` for a fresh login shell after installation. If needed, add `$HOME/.local/bin` to the installing account's shell PATH. Claude authentication is independent on each host.

### Choose the deployment values

The addresses below are examples. Substitute your actual reachable IPs or hostnames consistently, and use port `8080` unless you also update the service unit.

| Setting | Commander | Lieutenant |
| :--- | :--- | :--- |
| Stable agent ID | `atlas` | `nova` |
| Rank label | `Commander` | `Lieutenant` |
| Machine hostname | `atlas-host` | `nova-host` |
| Reachable LAN address | `10.10.0.10` | `10.10.0.20` |
| Communication role | `host` | `consumer` |
| Active vault path | `/srv/AtlasMemory` | `/mnt/atlas-vault` |
| Vault/share name | `AtlasMemory` | `AtlasMemory` |
| Vault grant | `write` | `read` |
| Task share | `AtlasTasks` | `AtlasTasks` |

Use different stable lowercase IDs composed of letters, digits, underscores, or hyphens. The IDs must match the token roster on both machines. Rank and reporting labels do not grant write access; the per-vault grants do.

Prepare `/srv/AtlasMemory` as a real vault in Obsidian on the Commander. Its root must contain `.obsidian`. The bundled system notes link to `Profile`, `Projects`, `Security MOC`, and `Working Style`; provide those real notes and resolve broken links before final validation.

Make the following connections reachable through your LAN, VPN, or other deliberately configured private network:

- Lieutenant → Commander: TCP `445` for SMB.
- Both machines → peer: the configured AgentComm API port, `8080` in this guide, for authenticated messaging and configuration synchronization.

A public AgentComm tunnel does not create an SMB mount. Remote vault indexing still needs actual filesystem access.

## 2. Install the Commander first

In the Commander's root shell, from the SKMR project directory:

```bash
python3 install.py --method B
```

You can also omit `--method` and select `B` at the menu. Accept or review package provisioning, enter `/srv/AtlasMemory` at the vault-path prompt, and optionally provide HackerOne API credentials from [API token settings](https://hackerone.com/settings/api_token/edit).

Enter your **HackerOne username** in the **API identifier** field (`H1_API_IDENTIFIER`), and your **generated API token value** in the **API token** field (`H1_API_TOKEN`). Personal Hacker API authentication uses this username/token pair; see the [official API token guide](https://docs.hackerone.com/en/articles/8410331-api-token). The installer saves them in the installing account's managed shell-profile block; the token prompt hides the value. Leaving them blank keeps bundled report data available but disables authenticated API refresh.

Answer the identity and connection prompts as follows:

| Prompt | Commander answer |
| :--- | :--- |
| This agent's ID / rank / hostname | `atlas` / `Commander` / `atlas-host` |
| Peer ID / rank / hostname | `nova` / `Lieutenant` / `nova-host` |
| Peer's LAN address | `10.10.0.20` |
| Vault name | `AtlasMemory` |
| Atlas's access / Nova's access | `write` / `read` |
| Who reports to whom | `peer-to-me` |
| Communication host or consumer | `host` |
| AgentComm port | `8080` |
| How THIS machine reaches the vault | `local` |
| Should the peer mount this vault over SMB? | `yes` |
| SMB server address | `10.10.0.10` |
| Vault path on SMB server | Leave blank for this Linux deployment. |
| Task share name | `AtlasTasks` |

`local` means that the files reside on this machine, even when it exports them to the peer. The installer creates the authenticated vault share and `/root/agentcomm/atlas.smb.auth`, with permissions `0600`. Because Nova's grant is READ, the exported vault share is read-only.

Initial role assignment is staged locally while the peer is being installed. The installer writes persistent identity and topology into `agent.conf`, working memory, and the managed `CLAUDE.md` block. Peer synchronization is completed later in this guide.

### Create the separate task share

The manual installer configures the vault share. For `/skmr:send task` to allocate task files, also configure a **writable task share**, separate from the read-only vault:

```bash
install -d -m 0770 /root/agentcomm/tasks
```

Add this section once to `/etc/samba/smb.conf`, keeping the vault share created by the installer:

```ini
[AtlasTasks]
    path = /root/agentcomm/tasks
    browseable = no
    read only = no
    guest ok = no
    valid users = atlas
    force user = root
    force group = root
    create mask = 0660
    directory mask = 0770
```

Validate and reload Samba, then verify both shares using the credential file:

```bash
testparm -s
smbcontrol all reload-config
smbclient //127.0.0.1/AtlasMemory -A /root/agentcomm/atlas.smb.auth -c ls
smbclient //127.0.0.1/AtlasTasks -A /root/agentcomm/atlas.smb.auth -c ls
```

The task-share name must match `smb_tasks_share` in both agents' configurations. Do not make the vault share writable to fix task allocation.

## 3. Export the tokens and transfer them to the other PC

**The option is `--extract-tokens`, not `--token-extract`.** It reads `/root/agentcomm/agent.conf` and saves `join.json` in the directory where the command runs. The output contains only the `tokens` roster and `config_admin_token`; it is written atomically with permissions `0600`.

Run this on the Commander in a private transfer directory:

```bash
install -d -m 0700 /root/skmr-transfer
cd /root/skmr-transfer
python3 /path/to/skmr/install.py --extract-tokens
stat -c '%a %n' join.json
```

The result must show `600 join.json`. To select another AgentComm installation, use:

```bash
python3 /path/to/skmr/install.py --extract-tokens --agentcomm-dir /path/to/agentcomm
```

Transfer **both** `join.json` and the Commander's SMB credential file through a private channel. AgentComm tokens and SMB credentials serve different purposes; a join file alone cannot authenticate a CIFS mount.

For example, assuming `workeruser` is an SSH account on the Lieutenant machine:

```bash
ssh workeruser@10.10.0.20 'install -d -m 0700 "$HOME/skmr-transfer"'
scp /root/skmr-transfer/join.json /root/agentcomm/atlas.smb.auth \
    workeruser@10.10.0.20:skmr-transfer/
```

Replace `workeruser` and the address with your SSH login. The destination is that account's home directory. On the Lieutenant, enter a root shell and install the received files:

```bash
sudo -i
install -d -m 0700 /etc/skmr
install -m 0600 /home/workeruser/skmr-transfer/join.json /root/join.json
install -m 0600 /home/workeruser/skmr-transfer/atlas.smb.auth /etc/skmr/atlas.smb.auth
```

Adjust `/home/workeruser` if the SSH account has a different home. Keep these files private and outside shared repositories; do not paste their contents into logs or documentation.

## 4. Mount the vault on the Lieutenant

The mount must exist **before** the Lieutenant's installation asks for the vault path. On an Ubuntu or Debian Lieutenant:

```bash
apt-get update
apt-get install -y cifs-utils
mkdir -p /mnt/atlas-vault
mount -t cifs //10.10.0.10/AtlasMemory /mnt/atlas-vault \
    -o credentials=/etc/skmr/atlas.smb.auth,ro,vers=3.0,uid=0,gid=0,file_mode=0440,dir_mode=0550
mountpoint /mnt/atlas-vault
findmnt -T /mnt/atlas-vault
ls /mnt/atlas-vault
```

Mount the share root: `//10.10.0.10/AtlasMemory`. The installer exports the vault itself, so do not append an extra `/Memory` directory to this example.

Confirm that `findmnt` reports a CIFS filesystem with `ro`. Samba intentionally hides `.obsidian`; the installer accepts a real remote mount without that marker. An unmounted `/mnt/atlas-vault` directory is not a valid substitute.

For a container-based manual deployment, the container also needs mount privileges such as `--cap-add SYS_ADMIN --cap-add DAC_READ_SEARCH --security-opt apparmor=unconfined`. Method B does not create that container for you.

### Restore the mount after reboot

On a systemd-based Linux Lieutenant, add an entry to `/etc/fstab` if you want the mount restored automatically:

```fstab
//10.10.0.10/AtlasMemory /mnt/atlas-vault cifs credentials=/etc/skmr/atlas.smb.auth,ro,vers=3.0,uid=0,gid=0,file_mode=0440,dir_mode=0550,_netdev,nofail,x-systemd.automount 0 0
```

Use your actual address and share name. Keep the credential file mode `0600`; avoid putting the password directly in `fstab`. After reboot, access the directory and check `mountpoint` and `findmnt` before using SKMR.

## 5. Install the Lieutenant using the join file

In the Lieutenant's root shell, from its SKMR project directory:

```bash
cd /path/to/skmr
python3 install.py --method B --join /root/join.json
```

The short form is equivalent:

```bash
python3 install.py --method B -j /root/join.json
```

Enter `/mnt/atlas-vault` as the vault path. Configure optional HackerOne credentials for this account if desired. Mirror the topology:

| Prompt | Lieutenant answer |
| :--- | :--- |
| This agent's ID / rank / hostname | `nova` / `Lieutenant` / `nova-host` |
| Peer ID / rank / hostname | `atlas` / `Commander` / `atlas-host` |
| Peer's LAN address | `10.10.0.10` |
| Vault name | `AtlasMemory` |
| Nova's access / Atlas's access | `read` / `write` |
| Who reports to whom | `me-to-peer` |
| Communication host or consumer | `consumer` |
| AgentComm port | `8080` |
| How THIS machine reaches the vault | `smb` |
| SMB server address | `10.10.0.10` |
| Vault path on SMB server | Leave blank for this Linux deployment. |
| Task share name | `AtlasTasks` |

The installer must report that the token pair was **adopted from the peer**. If it warns that the join-file identities do not match and generates a fresh pair, fix the IDs or join file; the two installations will otherwise reject each other's authenticated requests.

A READ agent may report that writer initialization is not permitted during the manual installer's verification. Initialize the vault on the WRITE host; do not grant the Lieutenant WRITE to bypass this restriction. Still resolve validation, indexing, connection, and doctor failures before using the deployment.

### Set the received SMB credential path and peer endpoints

On the Lieutenant, select the transferred credential file and canonical vault origin:

```bash
python3 - <<'PY'
import json
import os
from pathlib import Path

path = Path('/root/agentcomm/agent.conf')
config = json.loads(path.read_text(encoding='utf-8'))
config.update({
    'smb_credfile': '/etc/skmr/atlas.smb.auth',
    'smb_domain': 'atlas-host',
    'vault_origin': 'http://10.10.0.10:8080',
    'peer_api_lan': 'http://10.10.0.10:8080',
    'peer_admin_url': 'http://10.10.0.10:8080',
    'server_local_url': 'http://127.0.0.1:8080',
})
path.write_text(json.dumps(config, indent=2) + '\n', encoding='utf-8')
os.chmod(path, 0o600)
PY
```

Substitute your actual addresses before running this block. On the Commander, set its peer endpoints with:

```bash
python3 - <<'PY'
import json
import os
from pathlib import Path

path = Path('/root/agentcomm/agent.conf')
config = json.loads(path.read_text(encoding='utf-8'))
config.update({
    'peer_api_lan': 'http://10.10.0.20:8080',
    'peer_admin_url': 'http://10.10.0.20:8080',
    'server_local_url': 'http://127.0.0.1:8080',
})
path.write_text(json.dumps(config, indent=2) + '\n', encoding='utf-8')
os.chmod(path, 0o600)
PY
```

Both blocks preserve the token fields. Restart any running AgentComm service after changing its configuration.

## 6. Start AgentComm on both machines

The manual installer starts AgentComm on the communication host. Start the same service on the consumer as well so that its local API and peer administration endpoint are available.

For the default root installation and port `8080`, on **each** systemd host:

```bash
install -m 0644 /root/agentcomm/agentcomm.service /etc/systemd/system/agentcomm.service
systemctl daemon-reload
systemctl enable --now agentcomm
systemctl restart agentcomm
systemctl status agentcomm --no-pager
```

The shipped unit uses `/root/agentcomm` and port `8080`. If you selected different directories, an account, or a port, edit the unit's working directory, interpreter, and `ExecStart` accordingly. The packaged Claude command files also contain root paths; custom non-root deployments require reviewing those paths.

Without systemd, run the service in a separate terminal:

```bash
cd /root/agentcomm
python3 -m uvicorn server:app --host 0.0.0.0 --port 8080 --workers 1
```

First stop any earlier process occupying that port. A directly launched service needs your own supervision and restart procedure; method B does not provide the full-auto container startup script.

### Synchronize persistent roles

Once both services and their peer endpoints are reachable, run this on the Commander:

```bash
python3 /root/.claude/skmr/cli.py assign-role --non-interactive --sync-peer \
    --vault AtlasMemory --local-access write --remote-access read \
    --name atlas --role Commander --host atlas-host --reports-to '' \
    --remote-name nova --remote-role Lieutenant --remote-host nova-host \
    --remote-reports-to atlas --remote-lan 10.10.0.20 \
    --host-role host --remote-host-role consumer
```

Use the existing IDs and your actual hostnames. The configuration-admin secret authorizes synchronization; ordinary message tokens cannot grant WRITE. Require the command's peer-persistence confirmation. A synchronization failure or rollback is not a completed two-machine update.

Read the resulting topology on both hosts:

```bash
python3 /root/.claude/skmr/cli.py assign-role --show
```

## 7. Verify and open Claude

On both machines, from the source project directory:

```bash
python3 install.py --check
python3 /root/.claude/skmr/cli.py doctor
python3 /root/.claude/skmr/tests/run_all.py
```

`--check` verifies installed payload and configuration wiring. Doctor and the test suites check other parts of the system. The manual installer's final exit code alone is insufficient: some verification issues are recorded as warnings, so read the summary and resolve the underlying failures.

Verify and rebuild the retrieval index if necessary:

```bash
python3 /root/.claude/skmr/cli.py obsidian-memory validate
python3 /root/.claude/skmr/cli.py obsidian-memory index --no-plan
python3 /root/.claude/skmr/cli.py obsidian-memory search 'SKMR'
```

Run initialization on the WRITE Commander only if required:

```bash
python3 /root/.claude/skmr/cli.py obsidian-memory init
```

Open a fresh login shell to load the configured environment. Then launch Claude from `/srv/AtlasMemory` on the Commander and `/mnt/atlas-vault` on the Lieutenant:

```bash
bash -l
cd /srv/AtlasMemory
claude
```

Use the Lieutenant's mounted path in its own terminal. Inside each Claude session:

```text
/skmr:help
/skmr:assign-role --show
/skmr:doctor
/skmr:send health
/skmr:obsidian-memory search "SKMR"
/skmr:hackerone-reports pending
```

To send an explicitly chosen recommendation from Nova to Atlas:

```text
/skmr:send atlas "Recommendation: review the attached finding before preserving it."
```

A successful send confirms that AgentComm accepted and queued the message; it does not prove the peer processed it. The Commander validates proposed knowledge and uses the guarded preview → commit workflow. New report review also requires user authorization.

After successful joining, remove disposable transfer copies; retain the active SMB credential file:

```bash
# Commander
rm -f /root/skmr-transfer/join.json
```

```bash
# Lieutenant; use the actual SSH account's home directory
rm -f /root/join.json /home/workeruser/skmr-transfer/join.json /home/workeruser/skmr-transfer/atlas.smb.auth
```

## 8. What appears in Obsidian

The source is **`payload/vault/00 System/` inside the downloaded SKMR project**. All its contents are copied to `<selected-write-vault>/00 System/`:

```text
00 System/
├── Home.md
├── Knowledge Map.md
├── SondraMemory.md
└── SKMR/
    └── SKMR.md
```

Identical files are preserved, while differing existing files receive `.skmr-bak-*` backups before replacement. This copy happens on a WRITE vault; the Lieutenant reads the Commander's copy through the mounted share. The installer also initializes writer-managed source/log structures and builds the local retrieval index.

## Optional public messaging connection

When you need a public AgentComm connection, run this on the chosen host in Claude:

```text
/skmr:send connection public
```

Relay the **actual URL returned by that command** to the consumer:

```text
/skmr:send connection public https://THE_RETURNED_HOST_URL
```

Do not invent a tunnel URL or store a temporary public URL as the permanent LAN address. `/skmr:send connection private` selects private connectivity. Changing the message host does not move vault storage or change grants. A messaging tunnel does not provide CIFS filesystem access.

## Installer options

| Option | Meaning |
| :--- | :--- |
| `--method B` | Select manual installation without the method menu. |
| `--join FILE` / `-j FILE` | Adopt the existing peer's token roster and, when present, admin secret. |
| `--extract-tokens` | Export shared token fields into the current directory's `join.json`, mode `0600`. |
| `--agentcomm-dir PATH` | Select the installation/configuration directory; also selects the extraction source. |
| `--claude-dir PATH` | Select the Claude integration directory; review packaged command paths for custom layouts. |
| `--no-packages` | Skip the main system/Python package stage. Exporting a vault can still trigger Samba provisioning. |
| `--no-ollama` | Skip installing the embedding backend; lexical BM25 retrieval remains available. |
| `--no-prompts` | Install files and wiring only. It does not complete vault, API, identity, or network setup. |
| `--dry-run` | Show planned actions without installing or writing files. |
| `--check` | Check an existing installation; does not perform the complete doctor/test workflow. |
| `--rotate-secrets` | Replace existing transport/SMB secrets; the peer then needs a new join and refreshed SMB credentials. |
| `--uninstall` | Remove installer-managed SKMR payload, hooks, and policy after its confirmation prompt. |

Normal reinstallation preserves an existing matching token pair. Keep a backup of your configuration before making intentional identity, path, or secret changes. Uninstall leaves the Obsidian vault, `agent.conf`, knowledge repositories, and shell profile in place; it is not a complete data purge.

## Troubleshooting

| Symptom | Check and resolution |
| :--- | :--- |
| `claude` command missing | Install the CLI separately and verify the installing account's PATH. |
| HTTP `401` or role synchronization denied | Verify both sides use the same IDs, token roster, and configuration-admin secret. Re-export and rejoin if required. |
| Join file ignored | Look for a mismatch warning; the join roster must contain both exact stable IDs. |
| Mount permission denied | Check SMB reachability, the credential file, the exported share, and container mount capabilities where applicable. |
| Empty remote vault | Use `mountpoint` and `findmnt`; an unmounted directory must not be treated as a working vault. |
| Remote vault works, task allocation fails | Verify the separate task share, its configured name, and `smb_credfile` with `smbclient`. |
| Role label looks right but writes fail | Check named-vault grants with `assign-role --show`; labels do not confer WRITE. READ agents submit candidates to their WRITE peer. |
| AgentComm stops after reboot | Enable the systemd service or provide supervision for the direct uvicorn process. |
| Index/doctor warns about embeddings | Inspect `ollama list` and model availability; install `bge-m3` or deliberately use the documented BM25-only workflow. |
| System-note validation fails | Resolve missing link targets and invalid notes; use `obsidian-memory validate` to inspect the errors. |
| HackerOne credentials appear unavailable | Open a fresh login shell under the installation account. Do not print the token while diagnosing the environment. |

For **two Commanders with separate vaults and reciprocal read-only mounts**, [full auto](install_full_auto.md) provisions the entire topology. A custom manual deployment must separately configure both physical exports, mounts, storage origins, and per-vault grant maps; changing rank labels alone does not create that arrangement.
