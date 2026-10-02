# SKMR — Full Auto Installation

[Project overview](../README.md) · [Manual installation](install_manual.md)

**Method A provisions two Linux Docker labs and connects them to Claude Code, AgentComm, and persistent Obsidian memory.** The installer handles package installation, shared credentials, vault permissions, Samba exports, CIFS mounts, indexing, and verification. You provide agent identities, vault paths, optional HackerOne credentials, and then log in to Claude inside each lab.

## 1. Prepare the host

Run `install.py` on **Linux**, including a Linux distribution inside WSL. Have the complete SKMR project directory available, with `install.py`, `payload/`, and `knowledge/` together.

Required:

- Python 3.11 or newer on the host.
- A running Docker Engine that accepts Linux containers and is accessible from the shell running the installer.
- Internet access for container images, packages, Claude Code, Ollama, and the `bge-m3` model. The embedding backend is installed separately in both labs; allow enough disk space and memory for both.
- An existing Obsidian vault for every Commander, with its `.obsidian` directory present.

The installer creates the labs; it **does not install Docker on the host**. Follow the [official Docker Engine installation guide](https://docs.docker.com/engine/install/ubuntu/) if Docker is not ready.

Check the Linux host:

```bash
python3 --version
sudo docker version
sudo docker info --format '{{.OSType}}'
sudo docker ps -a --format 'table {{.Names}}\t{{.Status}}'
```

The operating-system result must be `linux`. The names `kali-lab` and `ubuntu-lab` must be available, including among stopped containers. The installer preserves existing containers and stops when either name is occupied.

The examples use `sudo docker`. If your current Linux account can already access the same Docker Engine without `sudo`, use `docker` consistently instead.

### Windows and WSL

Open the Linux distribution from PowerShell:

```powershell
wsl -d Ubuntu
```

Then use Linux paths inside that shell. For example:

```bash
cd '/mnt/c/Users/YOUR_WINDOWS_USER/OneDrive/Masaüstü/skmr'
```

A Windows vault such as `C:\Users\YOUR_WINDOWS_USER\Documents\AtlasMemory` is entered as `/mnt/c/Users/YOUR_WINDOWS_USER/Documents/AtlasMemory`. The Docker Engine used by this shell must be able to bind-mount that path. Do not enter a Windows drive-letter path at the Linux installer prompt.

### Prepare real vault content

Open each selected folder as a vault in Obsidian before installation. Method A checks for `.obsidian`; an empty placeholder directory is insufficient.

The bundled `00 System` notes contain links to **`Profile`**, **`Projects`**, **`Security MOC`**, and **`Working Style`**. These destination notes must exist in the selected vault and contain your actual information. Also resolve any other broken links in the vault. Full auto runs strict vault validation, so a vault with unresolved links can stop installation after the packages have been installed.

For two Commanders, choose **two distinct vault paths**. Each Commander owns its vault and receives read-only access to the other Commander's vault.

## 2. Start method A

From the project directory:

```bash
sudo python3 install.py --method A
```

Alternatively, run `sudo python3 install.py` and select `A` at the installation-method menu.

Enter the requested information in order:

1. **Agent 1's name.** Display names are normalized into safe, stable agent IDs; use the IDs shown by the installer when identifying configuration files and mount paths.
2. **Agent 1's rank:** `Commander` or `Lieutenant`.
3. **Agent 1's existing local vault path**, if it is a Commander.
4. **Agent 2's name**, different from the first agent's identity.
5. **Agent 2's rank:** `Commander` or `Lieutenant`.
6. **Agent 2's existing local vault path**, if it is a Commander. A Lieutenant's remote vault path is assigned automatically.
7. **HackerOne API identifier and token**, if you want authenticated disclosure discovery. Enter your **HackerOne username** in the **API identifier** field (`H1_API_IDENTIFIER`), and your **generated API token value** in the **API token** field (`H1_API_TOKEN`). Generate the token in [HackerOne API token settings](https://hackerone.com/settings/api_token/edit); the [official API token guide](https://docs.hackerone.com/en/articles/8410331-api-token) confirms this username/token pair. The token prompt hides the value. Leaving credentials blank retains the bundled report corpus but disables authenticated API refresh.

Two Lieutenants are rejected. These combinations are supported:

| Agent 1 | Agent 2 | Vault arrangement |
| :--- | :--- | :--- |
| Commander | Lieutenant | Commander owns one vault; Lieutenant mounts it read-only. |
| Lieutenant | Commander | Commander owns one vault; Lieutenant mounts it read-only. |
| Commander | Commander | Each owns a separate vault and mounts the other's vault read-only. |

**The first Commander is assigned to `kali-lab`, even when entered as Agent 2.** The other agent is assigned to `ubuntu-lab`.

## 3. What the installer provisions

| Component | Automatic configuration |
| :--- | :--- |
| Docker labs | `kali-lab` from `kalilinux/kali-rolling:latest`; `ubuntu-lab` from `ubuntu:24.04`. |
| Docker network | Managed `skmr-labs` network; actual container addresses and hostnames are detected. |
| Mount privileges | Both labs receive `SYS_ADMIN`, `DAC_READ_SEARCH`, and `apparmor=unconfined` for real CIFS mounts. |
| Dependencies | Required Linux and Python packages, Samba/CIFS tools, and supporting utilities. |
| Claude Code | Native CLI installed in each lab, plus SKMR commands, skills, hooks, and instructions. |
| Retrieval | Ollama and `bge-m3` in each lab; embedding inference is checked. |
| AgentComm | Services, the shared agent-token roster, separate configuration-admin secret, and connection settings. |
| Persistent identity | Agent names, ranks, reporting relationships, host roles, and per-vault grants in the configuration and managed instruction blocks. |
| Vault exports | Authenticated Samba shares and separate writable task shares. Peer vault shares are read-only. |
| Vault mounts | The actual CIFS mount is verified; a bare directory is not accepted as a successful connection. |
| Knowledge | Bundled security sources, disclosure-review queue, vault initialization, and retrieval index. |
| Verification | Installed-file checks, the shipped SKMR test suites, and doctor diagnostics in both labs. |

Provisioning starts with Kali and continues with Ubuntu. The installer exports the shared AgentComm credentials from Kali, transfers them through a private temporary host directory, and joins Ubuntu automatically. SMB credential files are transferred to the appropriate peer as well. Temporary join files are removed after provisioning; **you do not need to run `--extract-tokens` or supply `--join` for method A**.

Long-running download and provisioning steps display an animated `Loading, please wait ...` line. Wait for the final verification summary; reaching the container-creation step alone does not mean installation has finished.

### Vault paths and write authority

For a Commander named `atlas` and a Lieutenant named `nova`:

```text
Host's AtlasMemory vault
  └─ kali-lab /vault                  atlas: READ + WRITE
       └─ Samba share
            └─ ubuntu-lab /mnt/atlas-vault   nova: READ
```

For two Commanders:

```text
kali-lab                              ubuntu-lab
  /vault          own vault: WRITE      /vault          own vault: WRITE
  /mnt/nova-vault  peer vault: READ      /mnt/atlas-vault peer vault: READ
```

The exact peer mount names use the normalized agent IDs. In the Commander–Lieutenant topology, the Lieutenant reads shared knowledge and sends recommendations or knowledge candidates to the Commander. It cannot commit permanent notes or publish tasks requiring WRITE authority. Task exchange uses a separate task share; it does not require making the vault writable.

### The bundled `00 System` directory

The installer copies **all files beneath `payload/vault/00 System/`** into `00 System/` inside every selected WRITE vault:

```text
<your-vault>/00 System/
├── Home.md
├── Knowledge Map.md
├── SondraMemory.md
└── SKMR/
    └── SKMR.md
```

The source is the packaged project directory, not a separate personal vault on the installation host. Identical files are kept; differing existing files receive `.skmr-bak-*` backups before replacement. A Lieutenant reads the Commander's shared copy rather than writing its own copy through the read-only mount.

## 4. Enter the labs and authenticate Claude

The success summary prints entry commands and each agent's working directory. Open separate terminals:

```bash
sudo docker exec -it kali-lab bash -l
```

Inside Kali:

```bash
claude --version
claude auth login
cd /vault
claude
```

Open Ubuntu in the other terminal:

```bash
sudo docker exec -it ubuntu-lab bash -l
```

Inside Ubuntu, log in separately. For the example Lieutenant:

```bash
claude --version
claude auth login
cd /mnt/atlas-vault
claude
```

If Ubuntu contains a Commander, use `cd /vault` instead. Authentication requires your Claude account's browser flow; see the [official Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference). SKMR cannot perform the account login for you.

In each Claude session, check the installed integration:

```text
/skmr:help
/skmr:assign-role --show
/skmr:doctor
/skmr:send health
/skmr:obsidian-memory search "SKMR"
/skmr:hackerone-reports pending
```

Confirm that identities, reporting relationships, and grants match your choices. Report review is a separate user-authorized workflow; installing the corpus or filling the queue does not approve reviewing it automatically.

## 5. Verify from the host

You can rerun diagnostics without reinstalling:

```bash
sudo docker exec kali-lab python3 /opt/skmr-installer/install.py --check
sudo docker exec ubuntu-lab python3 /opt/skmr-installer/install.py --check
sudo docker exec kali-lab python3 /root/.claude/skmr/cli.py doctor
sudo docker exec ubuntu-lab python3 /root/.claude/skmr/cli.py doctor
```

`--check` checks installed files and wiring. Doctor checks subsystem health. Neither substitutes for running the shipped test suites:

```bash
sudo docker exec kali-lab python3 /root/.claude/skmr/tests/run_all.py
sudo docker exec ubuntu-lab python3 /root/.claude/skmr/tests/run_all.py
```

For the Lieutenant example, inspect the real mount:

```bash
sudo docker exec ubuntu-lab mountpoint /mnt/atlas-vault
sudo docker exec ubuntu-lab findmnt -T /mnt/atlas-vault
sudo docker exec ubuntu-lab ls /mnt/atlas-vault
```

It must be a CIFS mount with `ro` in its options. Full auto reports success only after its required checks complete; inspect any `[warn]` or `[FAIL]` output as well.

## 6. Restart and locate configuration

The containers use `unless-stopped`. Their `/usr/local/bin/skmr-lab-start` startup script restores Samba, Ollama, AgentComm, and configured CIFS mounts when they start again.

```bash
sudo docker stop kali-lab ubuntu-lab
sudo docker start kali-lab ubuntu-lab
```

Allow time for services and peer mounts to become available, then rerun doctor in both labs. Claude sessions are started separately after entering the containers.

| Location | Purpose |
| :--- | :--- |
| Host `~/.skmr/docker-install.json` | Saved topology plan, under the account running the installer. With `sudo`, normally `/root/.skmr/docker-install.json`. |
| Lab `/etc/skmr/auto-install.json` | Per-lab provisioning configuration; may include optional HackerOne credentials. |
| Lab `/opt/skmr-installer/` | Copy of the SKMR installation package. |
| Lab `/root/.claude/CLAUDE.md` | Managed SKMR instructions and persistent topology block. |
| Lab `/root/agentcomm/agent.conf` | Agent identities, grants, connection settings, and authentication secrets. |
| Lab `/etc/skmr/<peer-id>.smb.auth` | Credentials used to mount the peer's vault. |
| Lab `/root/.claude/state/installer-test-suites.log` | Full-auto test-suite output for diagnosis. |

Keep secret-bearing files private. The host plan does not carry the HackerOne credentials; the protected per-lab configuration can carry them.

## Troubleshooting

| Symptom | Check and resolution |
| :--- | :--- |
| Docker daemon unavailable | Confirm `sudo docker version` can reach the intended Linux Engine; start or configure Docker before rerunning. |
| Existing lab name rejected | Inspect `docker ps -a`. Method A does not overwrite existing labs or resume over their names. Keep them for diagnosis or explicitly remove an unwanted installation. |
| Existing `skmr-labs` network rejected | The installer accepts its own managed network, identified by `org.skmr.installer=auto`. Use a matching managed network; do not repurpose an unrelated network blindly. |
| Vault marker missing | Open the selected folder as a vault in Obsidian and verify `.obsidian` exists at its root. |
| Vault validation fails | Resolve the specific missing wiki-link targets or invalid notes reported by the validator, including the four destination notes described above. |
| CIFS mount fails | Check the reported SMB address, share, credentials, and kernel CIFS support. Confirm Docker allows the requested mount capabilities. Do not replace the mount with an empty directory. |
| Model download or inference fails | Check network access, available disk/memory, and `docker exec <lab> ollama list`; `bge-m3` must be available in each lab. |
| Credentials or requests rejected | Inspect the non-secret identity and peer settings; both agents must use the same token roster and admin secret. Never paste credentials into public logs. |
| Installation exits incomplete | Created labs remain available. Read the last failed stage and test log, repair the underlying issue, and rerun diagnostics. Do not assume the lab is healthy because its container is running. |

Method A requires the full interactive workflow. It cannot be combined with `--no-prompts`, `--no-packages`, `--no-ollama`, `--join`, or `--rotate-secrets`; use [method B](install_manual.md) for those operator-managed workflows.

### Explicit clean rebuild

If you intentionally want to discard both labs and start again, first preserve any container-local data you need. The following command deletes the containers and their writable layers. The host vault directories used as bind mounts remain on the host.

```bash
sudo docker rm -f kali-lab ubuntu-lab
```

Then launch method A again from the complete project directory. These reset commands are an operator choice, not a routine verification step.
