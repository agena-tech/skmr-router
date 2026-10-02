#!/usr/bin/env python3
"""SKMR installer -- provisions the full SKMR stack into Claude Code.

Run it from the directory that contains it:

    sudo python3 install.py                 # interactive
    sudo python3 install.py --method A      # two Docker labs, full auto
    sudo python3 install.py --method B      # manual Linux installation
    sudo python3 install.py --dry-run       # show every action, change nothing
    sudo python3 install.py --check         # verify an existing installation
    sudo python3 install.py --uninstall     # remove what this installer added

What it does, in order:

First choose the full Docker auto flow (A) or the existing manual flow (B).
Full auto collects names, Commander/Lieutenant ranks, local Commander vaults
and optional API credentials, then provisions and verifies both Linux labs.
At least one Commander is required. Each Commander writes only its own vault;
the peer's exported vault is always mounted read-only. Claude login is manual.

  1. prints the Agena mark,
  2. detects the platform package manager and installs the system packages
     SKMR needs (git, curl, cifs-utils, python3 tooling, cloudflared, ollama),
  3. installs the Python dependencies,
  4. copies the SKMR payload into the Claude configuration directory and the
     AgentComm directory,
  5. merges the SKMR hook and permission wiring into settings.json without
     discarding what is already there,
  6. places the bundled BugBountySkills and bugskill-ai repositories,
  7. asks for the Obsidian vault path and refuses a directory that is not a vault,
  8. asks for a HackerOne API credential, shows where to create one, and
     persists it to the user's shell profile,
  9. asks for the agent identity, the peer, and the per-vault READ/WRITE grants,
     persists them through the canonical role writer, and copies the complete
     bundled 00 System directory into the selected WRITE vault,
 10. verifies the result by running the shipped test suites and the doctor.

No step is reported as done unless it actually succeeded, and every file the
installer replaces is backed up next to the original first.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import tempfile
import unicodedata
import urllib.request
from contextlib import contextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAYLOAD = HERE / "payload"
KNOWLEDGE_SRC = HERE / "knowledge"
VAULT_SYSTEM_SRC = PAYLOAD / "vault" / "00 System"

RESET = "\033[0m"
NEON_GREEN = "\033[1;38;2;57;255;20m"
NEON_ORANGE = "\033[1;38;2;255;145;0m"
NEON_RED = "\033[1;38;2;255;49;49m"
DIM = "\033[2m"
BOLD = "\033[1m"

HACKERONE_TOKEN_URL = "https://hackerone.com/settings/api_token/edit"
OBSIDIAN_DOWNLOAD_URL = "https://obsidian.md/download"
BUGBOUNTY_URL = "https://github.com/0xN0RMXL/BugBountySkills.git"
BUGSKILL_URL = "https://github.com/SecurityTalent/bugskill-ai.git"

# Import name -> distribution name. Presence is checked by importing, because a
# recorded pip success does not prove the interpreter can load the module.
PYTHON_REQUIREMENTS = {
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "pydantic": "pydantic",
    "numpy": "numpy",
    "httpx": "httpx",
    "requests": "requests",
}

# Logical capability -> package name per manager. A missing entry means that
# manager has no such package, and the installer says so instead of guessing.
SYSTEM_PACKAGES = {
    "git": {"apt": "git", "dnf": "git", "yum": "git", "pacman": "git",
            "zypper": "git", "apk": "git", "brew": "git"},
    "curl": {"apt": "curl", "dnf": "curl", "yum": "curl", "pacman": "curl",
             "zypper": "curl", "apk": "curl", "brew": "curl"},
    "pip": {"apt": "python3-pip", "dnf": "python3-pip", "yum": "python3-pip",
            "pacman": "python-pip", "zypper": "python3-pip", "apk": "py3-pip"},
    "venv": {"apt": "python3-venv", "dnf": "python3-virtualenv",
             "zypper": "python3-virtualenv"},
    # Client side: mounting a share the other machine exports.
    "cifs": {"apt": "cifs-utils", "dnf": "cifs-utils", "yum": "cifs-utils",
             "pacman": "cifs-utils", "zypper": "cifs-utils", "apk": "cifs-utils"},
    "sqlite": {"apt": "sqlite3", "dnf": "sqlite", "yum": "sqlite",
               "pacman": "sqlite", "zypper": "sqlite3", "apk": "sqlite",
               "brew": "sqlite"},
    "jq": {"apt": "jq", "dnf": "jq", "yum": "jq", "pacman": "jq",
           "zypper": "jq", "apk": "jq", "brew": "jq"},
    # ollama's own installer extracts a zstd archive and aborts with
    # "This version requires zstd for extraction" when it is absent, which is
    # how the embedding backend failed to install on a Kali image.
    "zstd": {"apt": "zstd", "dnf": "zstd", "yum": "zstd", "pacman": "zstd",
             "zypper": "zstd", "apk": "zstd", "brew": "zstd"},
    # `ss`, used to inspect listening sockets on some paths; absent from slim images.
    "iproute2": {"apt": "iproute2", "dnf": "iproute", "yum": "iproute",
                 "pacman": "iproute2", "zypper": "iproute2", "apk": "iproute2"},
}

# name, availability probe, refresh argv, install argv prefix
MANAGERS = (
    ("apt", "apt-get", ["apt-get", "update", "-qq"], ["apt-get", "install", "-y", "-qq"]),
    ("dnf", "dnf", None, ["dnf", "install", "-y", "-q"]),
    ("yum", "yum", None, ["yum", "install", "-y", "-q"]),
    ("pacman", "pacman", ["pacman", "-Sy", "--noconfirm"],
     ["pacman", "-S", "--noconfirm", "--needed"]),
    ("zypper", "zypper", ["zypper", "--non-interactive", "refresh"],
     ["zypper", "--non-interactive", "install"]),
    ("apk", "apk", ["apk", "update"], ["apk", "add", "--no-cache"]),
    ("brew", "brew", None, ["brew", "install"]),
)

# Files that are templates or fragments: installed after substitution, or merged
# into an existing file, never copied verbatim to the destination tree.
TEMPLATES = {"claude/settings.skmr.json", "claude/CLAUDE.skmr.md",
             "agentcomm/agent.conf.template",
             "claude/skmr/config/skmr.json.template"}

# Server side: EXPORTING the vault so the peer machine can mount it. Installed
# only when this machine is the storage host, because a consumer needs the
# client alone and a Samba server it never uses is extra attack surface.
# smbclient is included so the installer can ASK smbd whether it is actually
# serving the share, instead of inferring that from a successful config write.
SMB_SERVER_PACKAGES = {
    "apt": ["samba", "samba-common-bin", "smbclient"],
    "dnf": ["samba", "samba-client"], "yum": ["samba", "samba-client"],
    "pacman": ["samba"], "zypper": ["samba", "samba-client"],
    "apk": ["samba", "samba-client"], "brew": [],
}

SMB_SHARE_START = "# >>> SKMR vault share >>>"
SMB_SHARE_END = "# <<< SKMR vault share <<<"

POLICY_START = "<!-- SKMR_POLICY_START -->"
POLICY_END = "<!-- SKMR_POLICY_END -->"
ENV_START = "# >>> SKMR environment >>>"
ENV_END = "# <<< SKMR environment <<<"


# --------------------------------------------------------------- presentation
def logo_lines() -> list[str]:
    """The Agena mark, the same one the SKMR runtime banner prints."""
    rows = [
        ("          _______  ", ""),
        ("         /       /", ""),
        ("___     /   ____/   ", ""),
        ("\\   \\  /   /\\", "            ___       ______   ______   _   __   ___ "),
        (" \\   \\/___/  \\", "          /   |     / ____/  / ____/  / | / /  /   | "),
        ("  \\       \\   \\", "        / /| |    / / __   / __/    /  |/ /  / /| | "),
        ("   \\_______\\   \\", "      / ___ |   / /_/ /  / /___   / /|  /  / ___ | "),
        ("           /   /", "     /_/  |_|   \\____/  /_____/  /_/ |_/  /_/  |_|   "),
        ("          /   /", "        "),
        ("          \\  /", "          Copyright (C) 2026, Agena Memory Systems "),
        ("           \\/", "           "),
    ]
    return [f"{NEON_RED}{mark}{RESET}{suffix}{RESET}" for mark, suffix in rows]


class Report:
    """Collects each step's real outcome so the summary cannot overstate it."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, state: str, name: str, detail: str = "") -> None:
        self.rows.append((state, name, detail))

    @property
    def failed(self) -> list[tuple[str, str, str]]:
        return [row for row in self.rows if row[0] == "fail"]

    @property
    def skipped(self) -> list[tuple[str, str, str]]:
        return [row for row in self.rows if row[0] == "skip"]

    def render(self) -> str:
        colour = {"ok": NEON_GREEN, "warn": NEON_ORANGE, "fail": NEON_RED, "skip": DIM}
        label = {"ok": "ok", "warn": "warn", "fail": "FAIL", "skip": "skip"}
        lines = []
        for state, name, detail in self.rows:
            mark = f"{colour[state]}{label[state]:>4}{RESET}"
            lines.append(f"  [{mark}] {name:<26} {DIM}{detail}{RESET}" if detail
                         else f"  [{mark}] {name}")
        return "\n".join(lines)


def say(message: str) -> None:
    print(message, flush=True)


def step(message: str) -> None:
    say(f"\n{BOLD}==>{RESET} {message}")


def note(message: str) -> None:
    say(f"    {DIM}{message}{RESET}")


def problem(message: str) -> None:
    say(f"    {NEON_RED}{message}{RESET}")


@contextmanager
def loading_indicator(enabled: bool):
    """Animate one terminal line while keeping redirected logs untouched."""
    stream = sys.stdout
    if not enabled or not stream.isatty():
        yield
        return
    label = "Loading, please wait"
    stopped = threading.Event()

    def render(dots: int) -> None:
        stream.write(f"\r    {DIM}{label} {'.' * dots:<3}{RESET}")
        stream.flush()

    def animate() -> None:
        dots = 1
        while not stopped.wait(0.35):
            dots = dots % 3 + 1
            render(dots)

    render(1)
    worker = threading.Thread(target=animate, name="skmr-loading", daemon=True)
    worker.start()
    try:
        yield
    finally:
        stopped.set()
        worker.join()
        stream.write("\r" + " " * (len(label) + 8) + "\r")
        stream.flush()


# ------------------------------------------------------------------- plumbing
def run(argv: list[str], *, timeout: int = 900, env: dict | None = None,
        cwd: str | None = None, loading: bool = False) -> subprocess.CompletedProcess:
    merged = dict(os.environ)
    if env:
        merged.update(env)
    try:
        with loading_indicator(loading):
            return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                  check=False, env=merged, cwd=cwd)
    except (OSError, subprocess.TimeoutExpired) as error:
        return subprocess.CompletedProcess(argv, 127, "", str(error))


def have(binary: str) -> bool:
    return shutil.which(binary) is not None


class NoMoreInput(Exception):
    """Standard input ended while a question was still open."""


def ask(prompt: str, default: str | None = None, *, secret: bool = False,
        allow_empty: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            if secret:
                import getpass
                value = getpass.getpass(f"    {prompt}{suffix}: ").strip()
            else:
                value = input(f"    {prompt}{suffix}: ").strip()
        except EOFError:
            # A piped or truncated answer list must not end in a traceback: the
            # operator needs to know which question was left unanswered.
            raise NoMoreInput(prompt) from None
        if not value and default is not None:
            return default
        if value or allow_empty:
            return value
        problem("A value is required.")


def ask_choice(prompt: str, options: list[str], default: str) -> str:
    rendered = "/".join(options)
    while True:
        value = ask(f"{prompt} ({rendered})", default).casefold()
        if value in options:
            return value
        problem(f"Choose one of: {rendered}")


def ask_yes(prompt: str, default: bool = True) -> bool:
    return ask_choice(prompt, ["yes", "no"], "yes" if default else "no") == "yes"


def backup(path: Path) -> Path | None:
    """Copy a file aside before it is replaced. Never silently overwrite."""
    if not path.exists():
        return None
    target = path.with_name(f"{path.name}.skmr-bak-{time.time_ns()}")
    shutil.copy2(path, target)
    return target


def atomic_write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def _nearest_existing(path: Path) -> Path:
    """The closest existing ancestor, so writability can be tested before mkdir."""
    for candidate in [path, *path.parents]:
        if candidate.exists():
            return candidate
    return Path("/")


def local_address() -> str:
    """This machine's LAN address, from the routing table; no packet is sent."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))          # reserved TEST-NET-1
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def substitute(text: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        text = text.replace(f"{{{{{key}}}}}", str(value))
    return text


def substitute_json(text: str, values: dict) -> str:
    return substitute(text, {key: json.dumps(str(value), ensure_ascii=False)[1:-1]
                             for key, value in values.items()})


# ------------------------------------------------------------ package manager
def detect_manager() -> tuple[str, list[str] | None, list[str]] | None:
    for name, probe, refresh, install in MANAGERS:
        if have(probe):
            return name, refresh, install
    return None


def install_system_packages(manager, report: Report, dry_run: bool) -> None:
    step("System packages")
    if manager is None:
        report.add("warn", "package manager",
                   "none of apt/dnf/yum/pacman/zypper/apk/brew found")
        problem("No supported package manager found. Install git, curl, python3-pip "
                "and cifs-utils yourself, then re-run with --check.")
        return
    name, refresh, install = manager
    note(f"package manager: {name}")

    wanted, unavailable = [], []
    for capability, mapping in SYSTEM_PACKAGES.items():
        package = mapping.get(name)
        (wanted if package else unavailable).append(package or capability)

    if dry_run:
        note(f"would install: {' '.join(wanted)}")
        report.add("skip", "system packages", "dry run")
        return

    if refresh:
        result = run(refresh, timeout=600, loading=True)
        if result.returncode != 0:
            report.add("warn", "package index",
                       f"refresh failed: {result.stderr.strip()[:70]}")

    result = run([*install, *wanted], timeout=1800, loading=True)
    if result.returncode == 0:
        report.add("ok", "system packages", f"{len(wanted)} packages via {name}")
    else:
        # One at a time, so a single unavailable name cannot block the rest.
        failed = [p for p in wanted if run([*install, p], timeout=900, loading=True).returncode != 0]
        if failed:
            report.add("warn", "system packages", f"unavailable: {' '.join(failed)}")
        else:
            report.add("ok", "system packages", f"{len(wanted)} packages via {name}")
    if unavailable:
        note(f"{name} has no package for: {', '.join(unavailable)} (not required)")


def install_cloudflared(manager, report: Report, dry_run: bool) -> None:
    """Needed only for `connection public`; its absence is not fatal."""
    if have("cloudflared"):
        report.add("ok", "cloudflared", "already present")
        return
    if dry_run:
        report.add("skip", "cloudflared", "dry run")
        return
    if platform.system() == "Darwin" and manager and manager[0] == "brew":
        if run(["brew", "install", "cloudflared"], timeout=900, loading=True).returncode == 0:
            report.add("ok", "cloudflared", "installed via brew")
            return
    arch = {"x86_64": "amd64", "amd64": "amd64",
            "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().casefold())
    if platform.system() == "Linux" and arch and have("curl"):
        url = ("https://github.com/cloudflare/cloudflared/releases/latest/download/"
               f"cloudflared-linux-{arch}")
        # /usr/local/bin needs root; an ordinary user gets it on their own PATH.
        target = (Path("/usr/local/bin/cloudflared") if os.geteuid() == 0
                  else Path(os.path.expanduser("~")) / ".local" / "bin" / "cloudflared")
        target.parent.mkdir(parents=True, exist_ok=True)
        if run(["curl", "-fsSL", "-o", str(target), url], timeout=600, loading=True).returncode == 0:
            os.chmod(target, 0o755)
            on_path = str(target.parent) in os.environ.get("PATH", "").split(os.pathsep)
            report.add("ok", "cloudflared",
                       f"downloaded for linux-{arch} to {target.parent}"
                       + ("" if on_path else " (not on PATH)"))
            return
        target.unlink(missing_ok=True)
    if have("snap") and run(["snap", "install", "cloudflared"], timeout=900, loading=True).returncode == 0:
        report.add("ok", "cloudflared", "installed via snap")
        return
    report.add("warn", "cloudflared",
               "not installed; `connection public` will be unavailable")


def ollama_responding() -> bool:
    probe = run(["bash", "-c",
                 "curl -fsS -m 3 http://127.0.0.1:11434/api/tags >/dev/null && echo yes"],
                timeout=30)
    return probe.stdout.strip() == "yes"


def start_ollama() -> bool:
    """Bring the embedding server up however this machine can.

    systemd first, then a plain background process, because a container has no
    systemd and `ollama pull` fails without a running server.
    """
    if ollama_responding():
        return True
    for argv in (["systemctl", "enable", "--now", "ollama"],
                 ["service", "ollama", "start"]):
        if have(argv[0]):
            if run(argv, timeout=180).returncode:
                continue
            for _ in range(15):
                if ollama_responding():
                    return True
                time.sleep(1)
    if have("ollama"):
        log = Path("/var/log/ollama-serve.log")
        try:
            handle = log.open("ab")
        except OSError:
            handle = subprocess.DEVNULL
        subprocess.Popen(["ollama", "serve"], stdout=handle, stderr=handle,
                         start_new_session=True)
        for _ in range(30):
            if ollama_responding():
                return True
            time.sleep(1)
    return ollama_responding()


def install_ollama(report: Report, dry_run: bool, wanted: bool) -> None:
    """Embedding backend. Without it, retrieval degrades to BM25 only."""
    if not wanted:
        report.add("skip", "ollama", "declined; retrieval will run BM25-only")
        return
    if have("ollama"):
        report.add("ok", "ollama", "already present")
    elif dry_run:
        report.add("skip", "ollama", "dry run")
        return
    elif have("curl"):
        run(["bash", "-c", "curl -fsSL https://ollama.com/install.sh | sh"], timeout=1800, loading=True)
        if not have("ollama"):
            report.add("warn", "ollama", "install failed; retrieval will run BM25-only")
            return
        report.add("ok", "ollama", "installed")
    else:
        report.add("warn", "ollama", "curl missing; cannot install")
        return
    if dry_run:
        return
    # `ollama pull` needs a running server. Installing ollama does not start one
    # on a machine without systemd, so pulling straight after installing failed
    # and left retrieval BM25-only -- with the vector suites failing as the only
    # visible symptom.
    if not start_ollama():
        report.add("warn", "ollama service",
                   "could not start; run `ollama serve` then `ollama pull bge-m3`")
        return
    report.add("ok", "ollama service", "responding on 127.0.0.1:11434")
    if run(["ollama", "pull", "bge-m3"], timeout=3600, loading=True).returncode != 0:
        report.add("warn", "embedding model",
                   "bge-m3 pull failed; retrieval will run BM25-only")
        return
    # Ask the server what it has, rather than trusting the pull's exit code.
    listed = run(["ollama", "list"], timeout=300).stdout
    if "bge-m3" in listed:
        report.add("ok", "embedding model", "bge-m3 pulled and listed by the server")
    else:
        report.add("warn", "embedding model", "pull reported success but the model is not listed")


def install_python_packages(report: Report, dry_run: bool) -> None:
    step("Python dependencies")
    missing = [dist for name, dist in PYTHON_REQUIREMENTS.items()
               if run([sys.executable, "-c", f"import {name}"], timeout=120).returncode != 0]
    if not missing:
        report.add("ok", "python packages", "all already importable")
        return
    note(f"missing: {' '.join(missing)}")
    if dry_run:
        report.add("skip", "python packages", "dry run")
        return
    for argv in ([sys.executable, "-m", "pip", "install", "--quiet", *missing],
                 [sys.executable, "-m", "pip", "install", "--quiet",
                  "--break-system-packages", *missing],
                 [sys.executable, "-m", "pip", "install", "--quiet", "--user", *missing]):
        if run(argv, timeout=1800, loading=True).returncode == 0:
            break
    # Verify by importing; a pip exit code is not proof the module loads.
    still = [name for name, dist in PYTHON_REQUIREMENTS.items()
             if dist in missing
             and run([sys.executable, "-c", f"import {name}"], timeout=120).returncode != 0]
    if still:
        report.add("fail", "python packages",
                   f"not importable after install: {' '.join(still)}")
    else:
        report.add("ok", "python packages", f"{len(missing)} installed")


# -------------------------------------------------------------------- payload
def payload_files() -> list[tuple[Path, str]]:
    """(source, relative destination) for every packaged file."""
    found = []
    for root, prefix in ((PAYLOAD / "claude", "claude"),
                         (PAYLOAD / "agentcomm", "agentcomm")):
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                found.append((path, f"{prefix}/{path.relative_to(root).as_posix()}"))
    return found


def destination_for(relative: str, claude_dir: Path, agentcomm_dir: Path) -> Path:
    if relative.startswith("claude/"):
        return claude_dir / relative[len("claude/"):]
    return agentcomm_dir / relative[len("agentcomm/"):]


def install_payload(claude_dir: Path, agentcomm_dir: Path, report: Report,
                    dry_run: bool) -> None:
    step("SKMR payload")
    files = [(s, r) for s, r in payload_files() if r not in TEMPLATES]
    if not files:
        report.add("fail", "payload", f"no files found under {PAYLOAD}")
        return
    written = replaced = unchanged = 0
    for source, relative in files:
        destination = destination_for(relative, claude_dir, agentcomm_dir)
        if dry_run:
            written += 1
            continue
        if destination.exists():
            if hashlib.sha256(destination.read_bytes()).hexdigest() == \
               hashlib.sha256(source.read_bytes()).hexdigest():
                unchanged += 1
                continue
            backup(destination)
            replaced += 1
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        written += 1
    detail = f"{written} written, {unchanged} already identical" + \
             (f", {replaced} replaced (backed up)" if replaced else "")
    report.add("skip" if dry_run else "ok", "payload",
               (detail + " [dry run]") if dry_run else detail)
    if dry_run:
        return

    for directory in (claude_dir / "bin", claude_dir / "skills/skmr/scripts"):
        for script in directory.glob("*.py"):
            os.chmod(script, 0o755)
    for script in (agentcomm_dir / "bootstrap.sh",):
        if script.exists():
            os.chmod(script, 0o755)
    for directory in (claude_dir / "state",
                      claude_dir / "backups",
                      claude_dir / "skmr" / "state",
                      claude_dir / "skmr" / "state" / "learning-candidates",
                      claude_dir / "projects" / "-root" / "memory"):
        directory.mkdir(parents=True, exist_ok=True)
    report.add("ok", "runtime directories", "state and backup directories present")


def install_vault_system(vault: Path | None, report: Report, dry_run: bool,
                         *, access: str) -> None:
    """Merge the complete bundled 00 System tree into the selected WRITE vault."""
    step("Obsidian — 00 System")
    if vault is None:
        report.add("skip", "vault system files", "no vault selected")
        return
    if access == "read":
        report.add("skip", "vault system files", "READ access; use the WRITE peer's shared files")
        return
    if access != "write":
        report.add("fail", "vault system files", "a configured WRITE grant is required")
        return
    source = VAULT_SYSTEM_SRC
    destination = vault / "00 System"
    try:
        if source.is_symlink() or not source.is_dir():
            raise OSError(f"bundled 00 System directory is missing or invalid: {source}")
        if not vault.is_dir() or not (vault / ".obsidian").is_dir():
            raise OSError(f"selected vault has no .obsidian directory: {vault}")
        entries = [source, *sorted(source.rglob("*"))]
        # Check the whole tree before copying, including empty directories and
        # hidden files. A destination link must not redirect a copy outside it.
        for entry in entries:
            target = destination / entry.relative_to(source)
            if entry.is_symlink() or target.is_symlink():
                raise OSError(f"cannot copy through a symlink: {entry} -> {target}")
            if not (entry.is_dir() or entry.is_file()):
                raise OSError(f"unsupported source entry: {entry}")
            if target.exists() and entry.is_dir() != target.is_dir():
                raise OSError(f"file/directory conflict at {target}")
        count = sum(entry.is_file() for entry in entries)
        if dry_run:
            report.add("skip", "vault system files", f"would copy all {count} files to {destination}")
            return
        written = unchanged = replaced = 0
        with loading_indicator(True):
            for entry in entries:
                target = destination / entry.relative_to(source)
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                digest = hashlib.sha256(entry.read_bytes()).digest()
                if target.exists():
                    if hashlib.sha256(target.read_bytes()).digest() == digest:
                        unchanged += 1
                        continue
                    backup(target)
                    replaced += 1
                # Keep a previous note intact if copying or verification fails.
                descriptor, name = tempfile.mkstemp(prefix=".skmr-system-", suffix=".tmp",
                                                     dir=target.parent)
                os.close(descriptor)
                temporary = Path(name)
                try:
                    shutil.copy2(entry, temporary)
                    if hashlib.sha256(temporary.read_bytes()).digest() != digest:
                        raise OSError(f"copied file did not match its source: {entry}")
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
                written += 1
        detail = f"{written} written, {unchanged} already identical"
        if replaced:
            detail += f", {replaced} replaced (backed up)"
        report.add("ok", "vault system files", f"{detail}; {destination}")
    except OSError as error:
        report.add("fail", "vault system files", str(error))


def install_config(claude_dir: Path, vault: Path | None, report: Report,
                   dry_run: bool) -> None:
    """Point the package's own config at this installation, not the build host.

    The packaged config carries the authoring machine's absolute paths. Left as
    shipped, an installation under a different --claude-dir silently reads and
    WRITES the build host's MEMORY.md, topology and CLAUDE.md -- which is exactly
    what a probe install did before this existed.
    """
    template = PAYLOAD / "claude" / "skmr" / "config" / "skmr.json.template"
    if not template.exists():
        report.add("fail", "skmr config", f"template missing at {template}")
        return
    text = substitute_json(template.read_text(encoding="utf-8"),
                      {"CLAUDE_DIR": str(claude_dir),
                       "VAULT_LOCAL": str(vault) if vault else "/mnt/skmr-vault"})
    leftover = sorted(set(re.findall(r"\{\{[A-Z_]+\}\}", text)))
    if leftover:
        report.add("fail", "skmr config", f"unfilled placeholders: {leftover}")
        return
    target = claude_dir / "skmr" / "config" / "skmr.json"
    if dry_run:
        report.add("skip", "skmr config", f"would write {target}")
        return
    backup(target)
    atomic_write(target, text)
    report.add("ok", "skmr config", f"paths bound to {claude_dir}")


def skmr_env(claude_dir: Path, agentcomm_dir: Path) -> dict[str, str]:
    """Every override the packaged code honours, so nothing escapes the target.

    The config file alone is not enough: a child process may resolve its own
    default before reading it, and the hooks read some paths from the
    environment directly.
    """
    return {
        "AGENTCOMM_CONF": str(agentcomm_dir / "agent.conf"),
        "AGENTCOMM_LIB": str(claude_dir / "lib"),
        "AGENTCOMM_CODE_ROOT": str(agentcomm_dir),
        "SKMR_CONFIG": str(claude_dir / "skmr" / "config" / "skmr.json"),
        "SKMR_RUNTIME_STATE": str(claude_dir / "skmr" / "state"),
        "SKMR_STATE_DIR": str(claude_dir / "state"),
        "SKMR_MEMORY_PATH": str(claude_dir / "projects" / "-root" / "memory" / "MEMORY.md"),
        "SKMR_AGENTS_PATH": str(claude_dir / "skmr" / "state" / "agents.json"),
        "SKMR_CLAUDE_MD": str(claude_dir / "CLAUDE.md"),
    }


def restore_vendor_modes(repository: Path) -> None:
    """Restore tracked executable bits after copying a bundle from Windows."""
    result = run(["git", "-C", str(repository), "ls-files", "--stage", "-z"], timeout=60)
    if result.returncode:
        raise RuntimeError("cannot read bundled repository file modes")
    root = repository.resolve()
    for entry in result.stdout.split("\0"):
        if not entry:
            continue
        metadata, name = entry.split("\t", 1)
        mode, _object, stage = metadata.split()
        if stage != "0" or mode not in ("100644", "100755"):
            continue
        path = repository / name
        if path.is_symlink() or not path.is_file():
            continue
        if not path.resolve().is_relative_to(root):
            raise RuntimeError("bundled repository contains an unsafe file path")
        path.chmod(0o755 if mode == "100755" else 0o644)


def install_knowledge(claude_dir: Path, report: Report, dry_run: bool) -> None:
    """Place the bundled repositories; clone only if the bundle omits them."""
    step("Knowledge repositories")
    knowledge_dir = claude_dir / "knowledge"
    if not dry_run:
        knowledge_dir.mkdir(parents=True, exist_ok=True)
    for name, url in (("BugBountySkills", BUGBOUNTY_URL),
                      ("bugskill-ai", BUGSKILL_URL),
                      ("bugskill-ai-data", None)):
        source = KNOWLEDGE_SRC / name
        destination = knowledge_dir / name
        if destination.is_dir() and any(destination.iterdir()):
            report.add("ok", name, "already present")
            continue
        if dry_run:
            report.add("skip", name,
                       "would place from bundle" if source.is_dir() else "would clone")
            continue
        if source.is_dir():
            shutil.copytree(source, destination, symlinks=False, dirs_exist_ok=True)
            head = ""
            if (destination / ".git").exists() and have("git"):
                restore_vendor_modes(destination)
                head = run(["git", "-C", str(destination), "rev-parse", "--short=7", "HEAD"],
                           timeout=60).stdout.strip()
            report.add("ok", name, f"placed from bundle{f' @ {head}' if head else ''}")
        elif url and have("git"):
            result = run(["git", "clone", "--depth", "1", url, str(destination)], timeout=1800, loading=True)
            report.add("ok" if result.returncode == 0 else "warn", name,
                       "cloned" if result.returncode == 0
                       else f"clone failed: {result.stderr.strip()[:60]}")
        else:
            report.add("warn", name, "not bundled and cannot be cloned")
            continue
        # The session hook refuses a managed repository owned by another
        # account. copytree and git clone already create these files as the
        # installing user, which is the account the agent will run as, so there
        # is nothing to chown -- and forcing uid 0 here, as this once did, made
        # the tree unusable for an ordinary user.


# ------------------------------------------------------------------- settings
def merge_hooks(existing: dict, addition: dict) -> tuple[dict, int]:
    """Add SKMR hook groups without touching or duplicating what is present."""
    merged = json.loads(json.dumps(existing)) if existing else {}
    added = 0
    for event, groups in addition.items():
        current = merged.setdefault(event, [])
        present = {json.dumps(h, sort_keys=True)
                   for group in current for h in group.get("hooks", [])}
        for group in groups:
            fresh = [h for h in group.get("hooks", [])
                     if json.dumps(h, sort_keys=True) not in present]
            if not fresh:
                continue
            new_group = {k: v for k, v in group.items() if k != "hooks"}
            new_group["hooks"] = fresh
            current.append(new_group)
            added += len(fresh)
    return merged, added


def install_settings(claude_dir: Path, agentcomm_dir: Path, vault: Path | None,
                     report: Report, dry_run: bool, *, extra_directories: tuple[str, ...] = ()) -> None:
    step("Claude Code integration")
    fragment_path = PAYLOAD / "claude" / "settings.skmr.json"
    if not fragment_path.exists():
        report.add("fail", "settings fragment", f"missing at {fragment_path}")
        return
    fragment = json.loads(substitute_json(fragment_path.read_text(encoding="utf-8"),
                                     {"CLAUDE_DIR": str(claude_dir),
                                      "AGENTCOMM_DIR": str(agentcomm_dir)}))
    settings_path = claude_dir / "settings.json"
    try:
        current = json.loads(settings_path.read_text(encoding="utf-8")) \
            if settings_path.exists() else {}
    except json.JSONDecodeError as error:
        report.add("fail", "settings.json", f"existing file is not valid JSON: {error}")
        return
    if not isinstance(current, dict):
        report.add("fail", "settings.json", "existing file is not a JSON object")
        return

    hooks, added = merge_hooks(current.get("hooks") or {}, fragment.get("hooks") or {})
    was = (current.get("permissions") or {}).get("allow", [])
    allow = list(dict.fromkeys([*was, *fragment.get("permissions", {}).get("allow", [])]))

    # The vault, the knowledge repositories and the AgentComm directory all sit
    # outside any project, so Claude cannot read them without being told they
    # are in scope. Missing, the whole system installs and then cannot open its
    # own memory.
    wanted_dirs = [str(claude_dir / "knowledge" / name) for name in
                   ("BugBountySkills", "bugskill-ai", "bugskill-ai-data")]
    if vault:
        wanted_dirs.append(str(vault))
    wanted_dirs.append(str(agentcomm_dir))
    wanted_dirs.extend(extra_directories)
    had_dirs = (current.get("permissions") or {}).get("additionalDirectories", [])
    directories = list(dict.fromkeys([*had_dirs, *wanted_dirs]))
    fragment.setdefault("permissions", {})["additionalDirectories"] = directories

    if dry_run:
        report.add("skip", "settings.json",
                   f"would add {added} hooks, {len(allow) - len(was)} permissions, "
                   f"{len(directories) - len(had_dirs)} directories")
        return
    updated = dict(current)
    updated["hooks"] = hooks
    permissions = dict(current.get("permissions") or {})
    permissions["allow"] = allow
    permissions["additionalDirectories"] = directories
    updated["permissions"] = permissions
    # Point Claude Code's own auto-memory at the directory SKMR keeps MEMORY.md
    # in. Left unset, Claude writes its working memory somewhere else and the
    # session hook's "where did we stop" state and Claude's own memory are two
    # different files that silently disagree.
    memory_dir = str(claude_dir / "projects" / "-root" / "memory")
    if updated.get("autoMemoryDirectory") not in (memory_dir,):
        updated["autoMemoryDirectory"] = memory_dir
    saved = backup(settings_path)
    atomic_write(settings_path, json.dumps(updated, indent=2, ensure_ascii=False) + "\n")
    # Record the fragment that was actually applied, with this installation's
    # paths. It is what the drift check compares settings.json against, so it
    # has to describe this machine rather than the one the package was built on.
    atomic_write(claude_dir / "SKMR_SETTINGS_FRAGMENT.json",
                 json.dumps(fragment, indent=2, ensure_ascii=False) + "\n")
    report.add("ok", "settings.json",
               f"+{added} hooks, +{len(allow) - len(was)} permissions, "
               f"{len(directories)} readable directories"
               + (f"; backup {saved.name}" if saved else ""))


def install_instructions(claude_dir: Path, vault: Path | None, peer: str | None,
                         report: Report, dry_run: bool) -> None:
    """Install the policy into CLAUDE.md, keeping any text already there.

    The policy names the canonical vault and the peer whose messages must be
    surfaced. Shipped verbatim it named the authoring host's vault and peer, so
    every installation read instructions about a machine it is not.
    """
    source = PAYLOAD / "claude" / "CLAUDE.skmr.md"
    if not source.exists():
        report.add("fail", "CLAUDE.md", f"policy template missing at {source}")
        return
    policy = substitute(source.read_text(encoding="utf-8"),
                        {"VAULT_LOCAL": str(vault) if vault else "/mnt/skmr-vault",
                         "PEER_LABEL": (peer or "peer").upper()})
    leftover = sorted(set(re.findall(r"\{\{[A-Z_]+\}\}", policy)))
    if leftover:
        report.add("fail", "CLAUDE.md", f"unfilled placeholders: {leftover}")
        return
    policy = policy.rstrip() + "\n"
    target = claude_dir / "CLAUDE.md"
    block = f"{POLICY_START}\n{policy}{POLICY_END}\n"
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    if POLICY_START in existing and POLICY_END in existing:
        if existing.index(POLICY_START) > existing.index(POLICY_END):
            report.add("fail", "CLAUDE.md", "policy delimiters are reversed; fix them by hand")
            return
        head, _, rest = existing.partition(POLICY_START)
        _, _, tail = rest.partition(POLICY_END)
        updated = head + block + tail.lstrip("\n")
        action = "policy block updated in place"
    elif policy.split("\n", 1)[0].strip() and policy.split("\n", 1)[0] in existing:
        # An earlier unmarked install (or the authoring host) already holds the
        # policy verbatim; wrapping it again would duplicate every rule.
        report.add("ok", "CLAUDE.md", "policy already present, left untouched")
        return
    else:
        updated = (existing.rstrip() + "\n\n" if existing.strip() else "") + block
        action = "policy block appended"
    if dry_run:
        report.add("skip", "CLAUDE.md", f"would be {action}")
        return
    saved = backup(target)
    atomic_write(target, updated)
    report.add("ok", "CLAUDE.md", action + (f"; backup {saved.name}" if saved else ""))


# ---------------------------------------------------------------------- vault
def prompt_vault(report: Report, dry_run: bool) -> Path | None:
    step("Obsidian vault")
    say("    SKMR stores permanent memory in an Obsidian vault.")
    note("A vault is a folder Obsidian has opened at least once; it then contains")
    note("a .obsidian configuration directory.")
    refused = 0
    while True:
        raw = ask("Path to your Obsidian vault", allow_empty=True)
        if not raw:
            if ask_yes("Skip the vault for now and configure it later?", False):
                report.add("warn", "obsidian vault",
                           "skipped; set vault_local in agent.conf later")
                return None
            continue
        # Resolved to an absolute path on purpose. A relative answer such as
        # "vault" was accepted and persisted verbatim, and every later process
        # then resolved it against its own working directory -- which for the
        # hooks and the writer is not the directory the operator typed it in.
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
            note(f"stored as an absolute path: {path}")
        if not path.exists():
            problem(f"{path} does not exist.")
            if dry_run:
                report.add("skip", "obsidian vault", "dry run")
                return None
            if ask_yes(f"Create {path} and use it as a new vault?", False):
                path.mkdir(parents=True, exist_ok=True)
            else:
                continue
        if not path.is_dir():
            problem(f"{path} is not a directory.")
            continue
        if (path / ".obsidian").is_dir():
            report.add("ok", "obsidian vault", str(path))
            return path
        if path.is_mount():
            # A mounted remote vault is attached by definition, and the marker
            # may legitimately be missing from the listing: this installer's own
            # export vetoes /.obsidian/, so the configuration never travels over
            # the share. Requiring the marker here rejected a correctly mounted
            # vault. The marker test belongs to a LOCAL directory, where the one
            # thing it can tell apart is a real vault from an empty stub.
            report.add("ok", "obsidian vault",
                       f"{path} (mounted; marker withheld by the export)")
            note("Mounted from another machine, so .obsidian is not expected here.")
            return path
        problem(f"{path} contains no .obsidian directory, so it is not an Obsidian vault.")
        say("    Please install Obsidian and open this folder as a vault, then try again:")
        say(f"      {BOLD}{OBSIDIAN_DOWNLOAD_URL}{RESET}")
        note(f"In Obsidian: 'Open folder as vault' -> choose {path}.")
        note("Obsidian creates .obsidian on first open.")
        refused += 1
        if refused >= 2 and ask_yes("Use this folder anyway, without Obsidian?", False):
            report.add("warn", "obsidian vault",
                       f"{path} has no .obsidian; Obsidian not verified")
            return path


# ------------------------------------------------------------------ hackerone
def shell_profile() -> Path:
    shell = Path(os.environ.get("SHELL", "/bin/bash")).name
    home = Path(os.path.expanduser("~"))
    if shell == "zsh":
        return home / ".zshrc"
    if shell == "fish":
        return home / ".config" / "fish" / "config.fish"
    return home / ".bashrc"


def persist_environment(values: dict[str, str], report: Report, dry_run: bool) -> None:
    """Write exports into the user's shell profile, inside one managed block."""
    profile = shell_profile()
    if profile.name == "config.fish":
        quoted = {k: "'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'" for k, v in values.items()}
        body = "\n".join(f"set -gx {k} {v}" for k, v in quoted.items())
    else:
        body = "\n".join(f"export {k}={shlex.quote(v)}" for k, v in values.items())
    block = f"{ENV_START}\n{body}\n{ENV_END}\n"
    existing = profile.read_text(encoding="utf-8") if profile.exists() else ""
    if ENV_START in existing and ENV_END in existing:
        head, _, rest = existing.partition(ENV_START)
        _, _, tail = rest.partition(ENV_END)
        updated = head + block + tail.lstrip("\n")
        action = f"updated the managed block in {profile.name}"
    else:
        # Above the interactivity guard, not at the end of the file. Debian and
        # Ubuntu .bashrc files open with `[ -z "$PS1" ] && return` (or the
        # equivalent `case $- in *i*) ;; *) return;; esac`), so a block appended
        # at the bottom is never reached by a NON-interactive shell -- which is
        # how hooks and services run. The credential was in the file and still
        # invisible to everything that needed it.
        guard = re.compile(r'^\s*(\[\s*-z\s*"\$PS1"\s*\]\s*&&\s*return'
                           r'|case\s+\$-\s+in)', re.MULTILINE)
        match = guard.search(existing)
        if match:
            cut = existing.rfind("\n", 0, match.start()) + 1
            updated = existing[:cut] + block + "\n" + existing[cut:]
            action = (f"inserted a managed block into {profile.name} above the "
                      "non-interactive guard")
        else:
            updated = (existing.rstrip() + "\n\n" if existing.strip() else "") + block
            action = f"appended a managed block to {profile.name}"
    if dry_run:
        report.add("skip", "shell profile", f"would have {action}")
        return
    profile.parent.mkdir(parents=True, exist_ok=True)
    backup(profile)
    atomic_write(profile, updated, mode=0o600)
    # Also export into THIS process, so every step the installer runs afterwards
    # inherits the credential. Without it the session hook ran without one,
    # recorded "H1_API_IDENTIFIER and H1_API_TOKEN are not defined", and then
    # its own six-hour cache replayed that verdict on a machine that had just
    # been given a working credential.
    os.environ.update(values)
    report.add("ok", "shell profile", action)


def prompt_hackerone(report: Report, dry_run: bool) -> None:
    step("HackerOne API credential")
    say("    The disclosed-report dataset refreshes through the HackerOne API.")
    say(f"    Create a token here: {BOLD}{HACKERONE_TOKEN_URL}{RESET}")
    note("You need the API identifier (username) and the API token. The token is")
    note("shown only once. Leave either empty to skip; the bundled dataset still works.")
    identifier = ask("HackerOne API identifier", allow_empty=True)
    if not identifier:
        report.add("skip", "hackerone api", "no credential given; dataset refresh disabled")
        return
    token = ask("HackerOne API token", secret=True, allow_empty=True)
    if not token:
        report.add("skip", "hackerone api", "no token given; dataset refresh disabled")
        return
    # These two names are not a choice: the session hook reads exactly
    # H1_API_IDENTIFIER and H1_API_TOKEN. Writing invented names persisted the
    # credential somewhere nothing would ever look, so the live API check kept
    # reporting "not configured" on a machine that had been given one.
    persist_environment({"H1_API_IDENTIFIER": identifier,
                         "H1_API_TOKEN": token}, report, dry_run)
    report.add("ok", "hackerone api", f"credential stored for {identifier}")


# ------------------------------------------------------------------- identity
def extract_tokens(config_path: Path, *, dry_run: bool = False) -> int:
    """Export just the shared join credentials into the current directory."""
    output = Path.cwd() / "join.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        joined = {"tokens": config["tokens"],
                  "config_admin_token": config["config_admin_token"]}
        if (not isinstance(joined["tokens"], dict) or not joined["tokens"]
                or not isinstance(joined["config_admin_token"], str)
                or not joined["config_admin_token"]):
            raise ValueError("agent.conf must contain tokens and a nonempty config_admin_token")
        if dry_run:
            note(f"would export tokens from {config_path} to {output} (mode 0600)")
            return 0
        # mkstemp creates the file as 0600 before any secret is written.
        descriptor, temporary = tempfile.mkstemp(prefix=".join-", suffix=".json", dir=output.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(joined, handle, indent=2)
                handle.write("\n")
            os.chmod(temporary, 0o600)
            os.replace(temporary, output)
        finally:
            Path(temporary).unlink(missing_ok=True)
    except (OSError, ValueError, KeyError, TypeError) as error:
        problem(f"Token export failed for {config_path}: {error}")
        return 1
    say(f"    {NEON_GREEN}Tokens exported{RESET}: {output} (mode 0600)")
    return 0


def load_join_secrets(path: Path | None, report: Report) -> dict:
    """Adopt the transport secrets of an already-installed peer.

    Two agents share one token pair: each authenticates to the other's service
    with it. Generating fresh secrets on the second machine produced two
    installations that could never authenticate to each other -- measured as
    both sides holding entirely different token pairs while every endpoint
    answered 200. Whoever installs second joins the existing pair.
    """
    if path is None:
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        report.add("fail", "join secrets", f"cannot read {path}: {error}")
        return {}
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict) or len(tokens) < 2:
        report.add("fail", "join secrets",
                   f"{path} has no usable 'tokens' object with both agents")
        return {}
    joined = {"tokens": {str(k).casefold(): str(v) for k, v in tokens.items()}}
    if payload.get("config_admin_token"):
        joined["config_admin_token"] = str(payload["config_admin_token"])
    return joined


def write_agent_conf(agentcomm_dir: Path, answers: dict, report: Report,
                     dry_run: bool, joined: dict | None = None,
                     rotate: bool = False) -> None:
    template = PAYLOAD / "agentcomm" / "agent.conf.template"
    if not template.exists():
        report.add("fail", "agent.conf", f"template missing at {template}")
        return
    # All placeholders occur inside JSON strings; a Windows path, quote or
    # Unicode display name must not corrupt the generated JSON.
    text = substitute_json(template.read_text(encoding="utf-8"), answers)
    leftover = sorted(set(re.findall(r"\{\{[A-Z_]+\}\}", text)))
    if leftover:
        report.add("fail", "agent.conf", f"unfilled placeholders: {leftover}")
        return
    conf = json.loads(text)
    if answers.get("SMB_CREDFILE"):
        conf["smb_credfile"] = answers["SMB_CREDFILE"]
    if answers.get("VAULT_PERMISSIONS"):
        conf["vault_permissions"] = answers["VAULT_PERMISSIONS"]
    for field in ("display_name", "peer_display_name", "peer_vault_local", "peer_vault_name", "vault_origin"):
        if field.upper() in answers:
            conf[field] = answers[field.upper()]
    conf["listen_port"] = int(conf["listen_port"])
    conf["storage_host"] = str(conf["storage_host"]).casefold() == "true"
    # Secrets are generated here, or adopted from the peer that installed first.
    # The package never ships a credential either way.
    self_id, peer_id = conf["self"], conf["peer"]
    joined = joined or {}
    shared = joined.get("tokens") or {}
    # An existing, working pair is preserved. Regenerating on every run rotated
    # the secrets out from under the peer: a second install on either machine
    # left the two holding different pairs, and every authenticated call started
    # answering 401 on a link that had just been proven to work. Rotation is now
    # something the operator asks for.
    existing = {}
    if not shared and not rotate:
        try:
            previous = json.loads((agentcomm_dir / "agent.conf").read_text(encoding="utf-8"))
            kept = previous.get("tokens")
            if isinstance(kept, dict) and set(kept) >= {self_id, peer_id}:
                existing = {"tokens": {k: str(v) for k, v in kept.items()},
                            "config_admin_token": previous.get("config_admin_token")}
        except (OSError, ValueError):
            existing = {}
    if existing:
        conf["tokens"] = {self_id: existing["tokens"][self_id],
                          peer_id: existing["tokens"][peer_id]}
        if existing.get("config_admin_token"):
            joined = {**joined, "config_admin_token": existing["config_admin_token"]}
        origin = "existing token pair preserved"
    elif shared.keys() >= {self_id, peer_id}:
        conf["tokens"] = {self_id: shared[self_id], peer_id: shared[peer_id]}
        origin = "adopted from the peer"
    else:
        if shared:
            report.add("warn", "join secrets",
                       f"the join file names {sorted(shared)}, not {sorted([self_id, peer_id])}; "
                       "generating a fresh pair instead")
        conf["tokens"] = {self_id: secrets.token_hex(32), peer_id: secrets.token_hex(32)}
        origin = "fresh pair generated"
    conf["token"] = conf["tokens"][self_id]
    conf["config_admin_token"] = (joined.get("config_admin_token")
                                  or secrets.token_urlsafe(48))
    target = agentcomm_dir / "agent.conf"
    if dry_run:
        report.add("skip", "agent.conf", f"would write {target}")
        return
    saved = backup(target)
    if saved:
        note(f"existing agent.conf backed up as {saved.name}")
    atomic_write(target, json.dumps(conf, indent=2) + "\n", mode=0o600)
    report.add("ok", "agent.conf", f"{target} (mode 0600, {origin})")


def prompt_identity(claude_dir: Path, agentcomm_dir: Path, vault: Path | None,
                    report: Report, dry_run: bool, joined: dict | None = None,
                    rotate: bool = False) -> dict:
    step("Agent identity and vault grants")
    say("    Two agents share one vault. Each has an id, a rank label, a host name,")
    say("    and a per-vault grant.")
    note("READ reads the vault. WRITE reads and writes it. WRITE/WRITE is valid;")
    note("READ/READ is rejected, because a vault with no writer can never be updated.")
    self_id = ask("This agent's id (lowercase, stable)", "agent1").casefold()
    self_role = ask("This agent's rank label", "Commander")
    self_host = ask("This machine's host name", platform.node() or "localhost")
    peer_id = ask("The peer agent's id", "agent2").casefold()
    while peer_id == self_id:
        problem("The peer id must differ from this agent's id.")
        peer_id = ask("The peer agent's id", "agent2").casefold()
    peer_role = ask("The peer's rank label", "Lieutenant")
    peer_host = ask("The peer's host name", f"{peer_id}-host")
    note("The LAN address is persisted. A public tunnel URL is never asked for:")
    note("it changes every session, so the host opens one on demand instead.")
    peer_lan = ask("The peer's LAN address (IP or hostname)")

    vault_name = ask("Vault name (the SMB share name)", "AgentMemory")
    self_access = ask_choice(f"{self_id}'s access to {vault_name}", ["write", "read"], "write")
    peer_access = ask_choice(f"{peer_id}'s access to {vault_name}", ["write", "read"], "read")
    while self_access == "read" and peer_access == "read":
        problem("READ/READ is rejected: a vault with no writer can never be updated.")
        self_access = ask_choice(f"{self_id}'s access to {vault_name}", ["write", "read"], "write")
        peer_access = ask_choice(f"{peer_id}'s access to {vault_name}", ["write", "read"], "read")
    # Who reports to whom is a real question, not something to assume. The
    # installer used to hardcode "the peer reports to this agent", which is
    # inverted whenever a subordinate is installed against a commander peer.
    note("A reporting relationship is a label, not a permission: grants above")
    note("decide access. 'neither' leaves both unattached.")
    reporting = ask_choice(f"Who reports to whom", ["peer-to-me", "me-to-peer", "neither"],
                           "peer-to-me")
    host_role = ask_choice("Is this machine the communication host or a consumer",
                           ["host", "consumer"], "host")
    port = ask("AgentComm port", "8080")

    # How the vault is physically provided decides which safety rule applies.
    # 'smb' requires a real mount, so an unmounted stub can never pass as an
    # empty vault. 'local' is for a vault that lives on this machine; it is
    # accepted only because the Obsidian marker was verified above, and a bare
    # stub has no marker. Guessing this from the filesystem would defeat both.
    # The question is how THIS machine reaches the vault, which is not the same
    # as how the peer reaches it. The machine that holds the files reads them
    # directly even when it exports them over SMB, so conflating the two made
    # the storage host demand a mount of its own share and fall back to HTTP.
    marker_seen = bool(vault and (vault / ".obsidian").is_dir())
    default_backing = "local" if marker_seen else "smb"
    note("'local' means the vault files are on this machine's own filesystem,")
    note("whether or not it also exports them. 'smb' means this machine mounts")
    note("the vault from the other machine.")
    backing = ask_choice("How does THIS machine reach the vault", ["local", "smb"],
                         default_backing)
    if backing == "local" and not marker_seen:
        problem("A local vault must be a verified Obsidian vault (.obsidian present).")
        backing = "smb"
        note("Falling back to 'smb'. Re-run once Obsidian has opened the folder.")

    exports = False
    if backing == "local":
        note("Only the machine holding the files can export them to the peer.")
        exports = ask_yes("Should the peer mount this vault from here over SMB?",
                          host_role == "host")

    smb_host = ask("Address of the SMB server that exports the vault",
                   local_address() if exports else "")
    windows_path = ask("Vault path on the SMB server (blank if not applicable)",
                       "", allow_empty=True)
    tasks_share = ask("Task share name", f"{peer_id.capitalize()}tasks")

    answers = {
        "SELF": self_id, "PEER": peer_id, "RANK": self_role,
        "PORT": port, "LOCAL_LAN": local_address(), "PEER_LAN": peer_lan,
        "SMB_HOST": smb_host, "VAULT_NAME": vault_name, "TASKS_SHARE": tasks_share,
        "SMB_DOMAIN": self_host,
        "AGENTCOMM_DIR": str(agentcomm_dir), "CLAUDE_DIR": str(claude_dir),
        "VAULT_LOCAL": str(vault) if vault else "",
        "VAULT_WINDOWS_PATH": windows_path,
        # A host talks to its own server; a consumer must address the PEER's.
        # Hardcoding 127.0.0.1 made a consumer query itself, so its vault reads
        # and its peer checks failed against an endpoint that serves nothing.
        "API_BASE": (f"http://127.0.0.1:{port}" if host_role == "host"
                     else f"http://{peer_lan}:{port}"),
        "VAULT_BACKING": backing,
        "SELF_ACCESS": self_access, "PEER_ACCESS": peer_access,
        "SELF_HOST_ROLE": host_role,
        "PEER_HOST_ROLE": "consumer" if host_role == "host" else "host",
        # Holding the files is what makes a machine the storage host; that is a
        # separate fact from hosting the message transport.
        "STORAGE_HOST": "true" if exports else "false",
        "EXPORTS_VAULT": "true" if exports else "false",
    }
    write_agent_conf(agentcomm_dir, answers, report, dry_run, joined, rotate)
    if dry_run:
        report.add("skip", "topology", "dry run")
        return answers

    # The canonical role writer owns the topology, agent.conf grants, MEMORY.md
    # and the delimited CLAUDE.md block. --local-only because the peer machine
    # is not reachable during a fresh install.
    argv = [sys.executable, str(claude_dir / "skmr" / "cli.py"), "assign-role",
            "--non-interactive", "--local-only",
            "--vault", vault_name,
            "--local-access", self_access, "--remote-access", peer_access,
            "--name", self_id, "--role", self_role, "--host", self_host,
            "--remote-name", peer_id, "--remote-role", peer_role,
            "--remote-host", peer_host,
            "--remote-lan", peer_lan,
            "--host-role", host_role, "--remote-host-role", answers["PEER_HOST_ROLE"]]
    # Both directions are set explicitly. An omitted flag keeps whatever the
    # topology already holds, so setting only one side against an existing
    # relationship produced "reporting cycle detected" when the peer was still
    # recorded as reporting to this agent.
    if reporting == "peer-to-me":
        argv += ["--reports-to", "", "--remote-reports-to", self_id]
    elif reporting == "me-to-peer":
        argv += ["--reports-to", peer_id, "--remote-reports-to", ""]
    else:
        argv += ["--reports-to", "", "--remote-reports-to", ""]
    result = run(argv, timeout=300, env=skmr_env(claude_dir, agentcomm_dir))
    if result.returncode == 0:
        report.add("ok", "topology",
                   f"{self_id} ({self_access}) / {peer_id} ({peer_access})")
    else:
        tail = (result.stderr or result.stdout).strip().splitlines()
        report.add("fail", "topology", tail[-1][:90] if tail else "assign-role failed")
    return answers


# --------------------------------------------------------------------- verify
def smb_listening() -> bool:
    """Whether something actually accepts SMB on 445.

    A process check is not the same thing: smbd can be up for seconds before it
    accepts, and `pgrep` reported "running" while the share probe was still
    being refused. `ss` is absent from some images, so this asks the socket.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(3)
    try:
        probe.connect(("127.0.0.1", 445))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def start_smb() -> bool:
    """Start smbd however this machine can: systemd, service, or directly.

    A container has no systemd, so systemctl alone would claim a working share
    on a machine serving none. Each attempt waits for the port to ACCEPT, not
    for the process to exist.
    """
    if smb_listening():
        return True
    for argv in (["systemctl", "enable", "--now", "smbd"],
                 ["service", "smbd", "restart"],
                 ["smbd", "--daemon"]):
        if not have(argv[0]):
            continue
        if run(argv, timeout=180).returncode:
            continue
        for _ in range(20):
            if smb_listening():
                return True
            time.sleep(1)
    return smb_listening()


def configure_samba(agentcomm_dir: Path, vault: Path | None, answers: dict,
                    manager, report: Report, dry_run: bool,
                    rotate: bool = False, *, non_interactive: bool = False) -> None:
    """Install and configure the Samba server that exports the vault.

    Only the storage host runs this. Until now the installer shipped cifs-utils
    (the client) and nothing that could EXPORT a share, so a two-machine setup
    got as far as "mount it" with nothing on the other end.
    """
    step("Vault share (Samba)")
    if not vault:
        report.add("skip", "samba", "no vault configured")
        return
    if answers.get("VAULT_BACKING") == "smb":
        note("This machine mounts the vault from the peer, so the peer exports it.")
        report.add("skip", "samba", "this machine is a client, not the vault host")
        return
    if answers.get("EXPORTS_VAULT") != "true":
        report.add("skip", "samba", "vault not exported; nothing to serve")
        return

    share = answers["VAULT_NAME"]
    account = answers["SELF"]
    say(f"    The peer mounts //{answers['LOCAL_LAN']}/{share} from this machine.")
    note("This needs a Samba server here, plus an SMB account the peer uses.")
    if not non_interactive and not ask_yes(f"Export {vault} as the '{share}' share?"):
        report.add("skip", "samba", "declined; export the vault yourself")
        return

    if dry_run:
        report.add("skip", "samba", f"would export {vault} as {share}")
        return
    if os.geteuid() != 0:
        report.add("warn", "samba", "needs root to install and configure; skipped")
        return

    if manager:
        name, _, install = manager
        packages = SMB_SERVER_PACKAGES.get(name, [])
        if packages and run([*install, *packages], timeout=1800, loading=True).returncode != 0:
            report.add("warn", "samba packages", f"could not install {' '.join(packages)}")
        elif packages:
            report.add("ok", "samba packages", " ".join(packages))
    if not have("smbd"):
        report.add("fail", "samba", "smbd not present after installation")
        return
    if not have("smbpasswd") or not have("pdbedit"):
        report.add("warn", "samba", "smbpasswd/pdbedit missing; cannot create the SMB account")

    # A unix account must back the SMB account, or smbpasswd has nothing to map.
    if run(["id", account], timeout=60).returncode != 0:
        run(["useradd", "--no-create-home", "--shell", "/usr/sbin/nologin", account], timeout=120)
    # Keep the existing SMB password unless rotation was asked for. Minting a
    # new one on every run invalidated the copy the peer had been given, so a
    # re-install silently broke the peer's read access with LOGON_FAILURE.
    credfile = agentcomm_dir / f"{account}.smb.auth"
    password = ""
    if not rotate and credfile.exists():
        for line in credfile.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("password="):
                password = line.split("=", 1)[1]
                break
    reused = bool(password)
    if not password:
        password = secrets.token_urlsafe(24)
    added = run(["bash", "-c",
                 f"printf '%s\\n%s\\n' \"$SKMR_SMB_PW\" \"$SKMR_SMB_PW\" | smbpasswd -s -a {account}"],
                timeout=180, env={"SKMR_SMB_PW": password})
    if added.returncode != 0:
        report.add("fail", "samba account",
                   (added.stderr or added.stdout).strip()[:70] or "smbpasswd failed")
        return
    run(["smbpasswd", "-e", account], timeout=120)

    # A vault inside someone's home directory cannot be traversed by a separate
    # SMB account: /root and /home/<user> are 0700. Serving the files as their
    # owner is the standard answer, and it keeps the vault's own permissions
    # intact instead of loosening the home directory for everyone.
    try:
        owner_uid = vault.stat().st_uid
        import pwd
        import grp
        owner = pwd.getpwuid(owner_uid).pw_name
        owner_group = grp.getgrgid(vault.stat().st_gid).gr_name
    except (OSError, KeyError):
        owner, owner_group = "root", "root"
    traversable = all(os.access(parent, os.X_OK) for parent in [vault, *vault.parents])
    if not traversable:
        note(f"{vault} sits under a directory only {owner} may enter, so the share")
        note(f"serves it as {owner} (force user). The vault's own modes are unchanged.")

    conf = Path("/etc/samba/smb.conf")
    block = "\n".join([
        SMB_SHARE_START,
        f"[{share}]",
        f"   path = {vault}",
        "   browseable = no",
        # The export must not outrank the grant. Exported writable, a peer
        # holding READ could write straight into the vault over SMB and bypass
        # both the canonical writer and its own refusal -- measured: the writer
        # denied `preview`, the HTTP route answered 403, and `smbclient put`
        # succeeded anyway. The share is writable only for a WRITE peer.
        f"   read only = {'no' if answers.get('PEER_ACCESS') == 'write' else 'yes'}",
        "   guest ok = no",
        f"   valid users = {account}",
        f"   force user = {owner}",
        f"   force group = {owner_group}",
        "   create mask = 0660",
        "   directory mask = 0770",
        # The vault is knowledge, not a download tree: keep dotfiles and the
        # Obsidian configuration out of the share's listing.
        "   veto files = /.obsidian/.smb.auth/",
        "   hide dot files = yes",
        SMB_SHARE_END,
        "",
    ])
    existing = conf.read_text(encoding="utf-8") if conf.exists() else "[global]\n   workgroup = WORKGROUP\n"
    if SMB_SHARE_START in existing and SMB_SHARE_END in existing:
        head, _, rest = existing.partition(SMB_SHARE_START)
        _, _, tail = rest.partition(SMB_SHARE_END)
        updated = head + block + tail.lstrip("\n")
        action = "share block updated in place"
    else:
        updated = existing.rstrip() + "\n\n" + block
        action = "share block appended"
    backup(conf)
    atomic_write(conf, updated)

    if have("testparm"):
        check = run(["testparm", "-s"], timeout=120)
        if check.returncode != 0:
            report.add("fail", "smb.conf", (check.stderr or "").strip()[-70:] or "invalid")
            return
    report.add("ok", "smb.conf", f"{action} for [{share}]")

    # The local agent's own credential file, for mounting the share from the
    # peer. It carries a password, so it is never printed and never packaged.
    atomic_write(credfile,
                 f"username={account}\npassword={password}\ndomain={answers['SMB_DOMAIN']}\n",
                 mode=0o600)
    report.add("ok", "smb credentials",
               f"{credfile} (mode 0600, "
               + ("existing password kept" if reused else "new password generated")
               + ", not printed)")

    if start_smb():
        report.add("ok", "smbd", "accepting connections on 445")
        # Prove the share is actually served, rather than assuming it from a
        # successful config write.
        if have("smbclient"):
            # Connect to the share and list it. `smbclient -L` was the wrong
            # probe: the share is deliberately `browseable = no`, so it is
            # absent from a server listing even when it is served correctly.
            served = run(["bash", "-c",
                          f"smbclient //127.0.0.1/{share} -U {account}%\"$SKMR_SMB_PW\" "
                          f"-c 'ls' 2>&1"],
                         timeout=120, env={"SKMR_SMB_PW": password})
            ok = served.returncode == 0 and "NT_STATUS" not in served.stdout.upper()
            report.add("ok" if ok else "warn", "share serves the vault",
                       f"[{share}] listed over SMB" if ok
                       else (served.stdout or "").strip().splitlines()[-1][:70])
    else:
        report.add("warn", "smbd",
                   "installed and configured but not running; start it with "
                   "`systemctl enable --now smbd` or `smbd --daemon`")
    say(f"    {DIM}The peer mounts it with:{RESET}")
    say(f"      mount -t cifs //{answers['LOCAL_LAN']}/{share} /mnt/{share.casefold()} "
        f"-o credentials=<its own copy of {credfile.name}>,ro,vers=3.0")
    note(f"Copy {credfile} to the peer over a channel you trust. Never commit it.")


def materialise_skills(claude_dir: Path, agentcomm_dir: Path, report: Report,
                       dry_run: bool) -> None:
    """Run the session hook once so the managed skills actually exist.

    Claude installs them at session start, which means a freshly installed tree
    has no skills and no inventory until the operator happens to open a session.
    Running it here makes the installation complete rather than merely staged.
    """
    step("Security skills")
    # The dry-run check comes first: on a dry run the payload was never written,
    # so a missing hook is the expected state, not a failure to report.
    if dry_run:
        report.add("skip", "security skills", "dry run")
        return
    hook = claude_dir / "bin" / "security-kb-session-start.py"
    if not hook.exists():
        report.add("fail", "security skills", f"hook missing at {hook}")
        return
    event = json.dumps({"source": "startup", "session_id": "skmr-install"})
    result = subprocess.run([sys.executable, str(hook)], input=event, text=True,
                            capture_output=True, timeout=1800, check=False,
                            env={**os.environ, **skmr_env(claude_dir, agentcomm_dir)})
    inventory = claude_dir / "state" / "security-managed-skills.json"
    if inventory.exists():
        try:
            names = json.loads(inventory.read_text(encoding="utf-8")).get("skills", [])
        except (OSError, json.JSONDecodeError):
            names = []
        report.add("ok", "security skills", f"{len(names)} managed skills installed")
    else:
        detail = (result.stderr or result.stdout).strip().splitlines()
        report.add("warn", "security skills",
                   f"no inventory written; hook exit {result.returncode}"
                   + (f": {detail[-1][:50]}" if detail else ""))


def agentcomm_listening(port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(3)
    try:
        probe.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def start_agentcomm(agentcomm_dir: Path, port: int, report: Report,
                    dry_run: bool, *, managed_restart: bool = False) -> None:
    """Start the messaging service, so /skmr:send works without a manual step.

    Only the machine hosting the transport runs one. The installer used to leave
    this entirely to the operator, so a finished installation could not send
    anything until someone found the uvicorn command by hand.
    """
    step("AgentComm service")
    unit = agentcomm_dir / "agentcomm.service"
    if dry_run:
        report.add("skip", "agentcomm service", "dry run")
        return
    if agentcomm_listening(port):
        report.add("ok", "agentcomm service", f"already accepting on {port}")
        return
    # systemd where there is one, so the service survives a reboot.
    if have("systemctl") and unit.exists():
        target = Path("/etc/systemd/system/agentcomm.service")
        if not target.exists():
            try:
                shutil.copy2(unit, target)
                run(["systemctl", "daemon-reload"], timeout=120)
            except OSError:
                pass
        started = run(["systemctl", "enable", "--now", "agentcomm"], timeout=180)
        for _ in range(15 if started.returncode == 0 else 0):
            if agentcomm_listening(port):
                report.add("ok", "agentcomm service", f"systemd unit active on {port}")
                return
            time.sleep(1)
    # No systemd (a container): run it directly and say so, because nothing
    # will bring it back after a restart.
    log = agentcomm_dir / "agentcomm.log"
    try:
        handle = log.open("ab")
    except OSError:
        handle = subprocess.DEVNULL
    subprocess.Popen([sys.executable, "-m", "uvicorn", "server:app",
                      "--host", "0.0.0.0", "--port", str(port), "--workers", "1"],
                     cwd=str(agentcomm_dir), stdout=handle, stderr=handle,
                     start_new_session=True)
    for _ in range(180 if managed_restart else 20):
        if agentcomm_listening(port):
            report.add("ok" if managed_restart else "warn", "agentcomm service",
                       f"started on {port}; Docker startup restores it after restart" if managed_restart
                       else f"started directly on {port}; no systemd here, so restart it yourself after a reboot")
            return
        time.sleep(1)
    tail = ""
    if log.exists():
        lines = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        tail = lines[-1][:60] if lines else ""
    report.add("fail", "agentcomm service",
               f"not accepting on {port}" + (f": {tail}" if tail else ""))


def seed_review_queue(claude_dir: Path, agentcomm_dir: Path, report: Report,
                      dry_run: bool, count: int = 45) -> None:
    """Put the most recent disclosures into the review queue.

    The queue is the backlog of disclosures the operator has not looked at, and
    the session hook only ever adds reports it discovers as NEW since its last
    checkpoint. On a fresh machine the bundled dataset is already current, so
    nothing is new, the queue stays empty and the 'shall I review the new
    reports?' offer never fires at all -- the installed system looks different
    from a running one for no reason other than history. A new installation has
    reviewed nothing, so the recent disclosures genuinely are its backlog.
    """
    step("Disclosure review queue")
    hook = claude_dir / "bin" / "security-kb-session-start.py"
    dataset = claude_dir / "knowledge" / "bugskill-ai-data" / "hackerone_public_reports.json"
    if dry_run:
        report.add("skip", "review queue", "dry run")
        return
    if not hook.exists() or not dataset.exists():
        report.add("warn", "review queue", "hook or dataset missing; nothing seeded")
        return
    existing = claude_dir / "state" / "security-new-reports.json"
    if existing.exists():
        try:
            already = len(json.loads(existing.read_text(encoding="utf-8")).get("reports", []))
        except (OSError, json.JSONDecodeError):
            already = 0
        if already:
            report.add("ok", "review queue", f"{already} already queued; left alone")
            return

    # Seeded through the hook's own summariser and queue writer, so the entries
    # are byte-identical to the ones a live discovery would produce.
    script = f'''
import importlib.util, json, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("kb", {str(hook)!r})
kb = importlib.util.module_from_spec(spec)
sys.argv = ["kb"]
spec.loader.exec_module(kb)
reports = json.loads(Path({str(dataset)!r}).read_text(encoding="utf-8"))["reports"]
dated = [r for r in reports if (r.get("attributes") or {{}}).get("disclosed_at")]
dated.sort(key=lambda r: r["attributes"]["disclosed_at"], reverse=True)
print(kb.queue_new_reports(dated[:{count}]))
'''
    result = run([sys.executable, "-c", script], timeout=600,
                 env=skmr_env(claude_dir, agentcomm_dir))
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        report.add("warn", "review queue",
                   detail[-1][:70] if detail else f"exit {result.returncode}")
        return
    try:
        queued = len(json.loads(existing.read_text(encoding="utf-8")).get("reports", []))
    except (OSError, json.JSONDecodeError):
        queued = 0
    report.add("ok" if queued else "warn", "review queue",
               f"{queued} recent disclosures queued for review" if queued
               else "nothing queued")


def record_doctor(report: Report, returncode: int, stdout: str, *, strict: bool = False) -> None:
    plain = re.sub(r"\x1b\[[0-9;]*m", "", stdout)
    rows = [line.strip() for line in plain.splitlines()
            if re.match(r"^\s*\[\s*(?:ok|warn|FAIL)\s*\]\s+", line, re.I)]
    bad = [line for line in rows if re.match(r"^\[\s*FAIL\s*\]", line, re.I)]
    incomplete = returncode != 0 or not rows or (strict and len(rows) < 8)
    degraded = any("warn" in line.casefold() for line in rows)
    state = ("fail" if strict else "warn") if bad or incomplete else ("warn" if degraded else "ok")
    report.add(state, "doctor", f"{len(rows)} checks, {len(bad)} failing; exit {returncode}")
    for line in rows:
        note(line)


def verify(claude_dir: Path, agentcomm_dir: Path, vault: Path | None,
           report: Report, dry_run: bool, *, strict: bool = False,
           initialise: bool = True) -> None:
    step("Verification")
    if dry_run:
        report.add("skip", "verification", "dry run")
        return
    env = skmr_env(claude_dir, agentcomm_dir)

    writer = claude_dir / "skills" / "skmr" / "scripts" / "obsidian_memory.py"
    if vault and writer.exists() and initialise:
        init = run([sys.executable, str(writer), "init"], timeout=300, env=env)
        report.add("ok" if init.returncode == 0 else ("fail" if strict else "warn"), "vault initialised",
                   "PARA layout present" if init.returncode == 0
                   else (init.stderr or init.stdout).strip()[:70])
        check_result = run([sys.executable, str(writer), "validate"], timeout=300, env=env)
        if check_result.returncode == 0:
            try:
                notes = json.loads(check_result.stdout).get("notes", "?")
            except json.JSONDecodeError:
                notes = "?"
            report.add("ok", "vault validate", f"{notes} notes, no errors")
        else:
            report.add("fail" if strict else "warn", "vault validate",
                       (check_result.stderr or check_result.stdout).strip()[:70])

    # Build the retrieval index. Without this the vault is installed but not
    # searchable: doctor reports an empty index and every recall falls through,
    # which looks like "retrieval does not work" rather than "nothing is indexed
    # yet". It runs after init/validate so it indexes a vault known to be sound.
    cli = claude_dir / "skmr" / "cli.py"
    if vault and cli.exists():
        built = run([sys.executable, str(cli), "obsidian-memory", "index", "--no-plan"],
                    timeout=3600, env=env)
        probe = run([sys.executable, "-c",
                     f"import sys; sys.path.insert(0, {str(claude_dir)!r});"
                     "from skmr.memory.indexing import index;"
                     "s = index.stats();"
                     "print(s['files'], s['chunks'], s['vectors'])"],
                    timeout=300, env=env)
        numbers = probe.stdout.split()
        if built.returncode == 0 and len(numbers) == 3:
            files, chunks, vectors = numbers
            report.add("ok", "retrieval index",
                       f"{files} files, {chunks} chunks, {vectors} vectors"
                       + ("" if vectors != "0" else " (BM25 only; no embedding backend)"))
        else:
            detail = (built.stderr or built.stdout).strip().splitlines()
            report.add("fail" if strict else "warn", "retrieval index",
                       detail[-1][:70] if detail else "index build failed")

    suites = claude_dir / "skmr" / "tests" / "run_all.py"
    if suites.exists():
        result = run([sys.executable, str(suites)], timeout=2400, env=env)
        lines = (result.stdout or "").strip().splitlines()
        summary = next((l for l in reversed(lines) if "suites" in l), "")
        report.add("ok" if result.returncode == 0 else "fail", "test suites",
                   summary[:70] or f"exit {result.returncode}")
        if strict:
            atomic_write(claude_dir / "state" / "installer-test-suites.log",
                         (result.stdout or "") + (result.stderr or ""), mode=0o600)
        if result.returncode:
            for line in ((result.stdout or "") + (result.stderr or "")).splitlines()[-100:]:
                note(line)

    cli = claude_dir / "skmr" / "cli.py"
    if cli.exists():
        result = run([sys.executable, str(cli), "doctor"], timeout=600, env=env)
        record_doctor(report, result.returncode, result.stdout or "", strict=strict)


# ------------------------------------------------------------------ uninstall
def uninstall(claude_dir: Path, agentcomm_dir: Path, report: Report,
              dry_run: bool) -> None:
    step("Uninstall")
    say(f"    {NEON_ORANGE}This removes the SKMR files, hooks and policy block.{RESET}")
    note("Your Obsidian vault, agent.conf, knowledge repositories and shell")
    note("profile are left alone.")
    if not dry_run and not ask_yes("Proceed?", False):
        report.add("skip", "uninstall", "declined")
        return
    removed = 0
    for _, relative in payload_files():
        if relative in TEMPLATES:
            continue
        target = destination_for(relative, claude_dir, agentcomm_dir)
        if target.exists():
            if not dry_run:
                target.unlink()
            removed += 1

    settings_path = claude_dir / "settings.json"
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            hooks = data.get("hooks") or {}
            for event, groups in list(hooks.items()):
                kept = []
                for group in groups:
                    inner = [h for h in group.get("hooks", [])
                             if f"{claude_dir}/bin/" not in json.dumps(h)]
                    if inner:
                        new = {k: v for k, v in group.items() if k != "hooks"}
                        new["hooks"] = inner
                        kept.append(new)
                if kept:
                    hooks[event] = kept
                else:
                    hooks.pop(event)
            data["hooks"] = hooks
            if not dry_run:
                backup(settings_path)
                atomic_write(settings_path,
                             json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    instructions = claude_dir / "CLAUDE.md"
    if instructions.exists():
        text = instructions.read_text(encoding="utf-8")
        if POLICY_START in text and POLICY_END in text and not dry_run:
            head, _, rest = text.partition(POLICY_START)
            _, _, tail = rest.partition(POLICY_END)
            backup(instructions)
            atomic_write(instructions, head.rstrip() + "\n" + tail.lstrip("\n"))
    report.add("skip" if dry_run else "ok", "uninstall",
               f"{removed} files {'would be ' if dry_run else ''}removed")


# ---------------------------------------------------------------------- check
def check(claude_dir: Path, agentcomm_dir: Path, report: Report) -> None:
    step("Installation check")
    missing, differing, present = [], [], 0
    for source, relative in payload_files():
        if relative in TEMPLATES:
            continue
        target = destination_for(relative, claude_dir, agentcomm_dir)
        if not target.exists():
            missing.append(relative)
        elif hashlib.sha256(target.read_bytes()).hexdigest() != \
                hashlib.sha256(source.read_bytes()).hexdigest():
            differing.append(relative)
        else:
            present += 1
    report.add("ok" if not missing else "fail", "payload files",
               f"{present} identical, {len(differing)} modified, {len(missing)} missing")
    for name in missing[:10]:
        problem(f"missing: {name}")
    for name in differing[:10]:
        note(f"modified locally: {name}")

    settings_path = claude_dir / "settings.json"
    if settings_path.exists():
        wired = settings_path.read_text(encoding="utf-8").count(f"{claude_dir}/bin/")
        report.add("ok" if wired else "fail", "hooks wired",
                   f"{wired} SKMR hook references")
    else:
        report.add("fail", "hooks wired", "settings.json is absent")

    instructions = claude_dir / "CLAUDE.md"
    if instructions.exists():
        text = instructions.read_text(encoding="utf-8")
        has_policy = POLICY_START in text or "SKMR Policy" in text
        report.add("ok" if has_policy else "fail", "CLAUDE.md policy",
                   "present" if has_policy else "no SKMR policy found")
    else:
        report.add("fail", "CLAUDE.md policy", "CLAUDE.md is absent")

    conf = agentcomm_dir / "agent.conf"
    if conf.exists():
        try:
            data = json.loads(conf.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            report.add("fail", "agent.conf", f"not valid JSON: {error}")
            return
        report.add("ok", "agent.conf",
                   f"self={data.get('self')} grants="
                   f"{json.dumps(data.get('vault_permissions', {}))}"[:70])
        mode = oct(conf.stat().st_mode & 0o777)
        report.add("ok" if mode == "0o600" else "warn", "agent.conf mode", mode)
        vault_local = data.get("vault_local")
        if vault_local:
            path = Path(vault_local)
            # Same rule as the prompt: a mounted vault need not show .obsidian,
            # because the exporting side withholds it.
            if (path / ".obsidian").is_dir():
                report.add("ok", "vault path", str(path))
            elif path.is_mount():
                report.add("ok", "vault path", f"{path} (mounted from the peer)")
            else:
                report.add("warn", "vault path", f"{path} (no .obsidian)")
    else:
        report.add("fail", "agent.conf", f"absent at {conf}")

    for name in ("BugBountySkills", "bugskill-ai"):
        path = claude_dir / "knowledge" / name
        report.add("ok" if path.is_dir() and any(path.iterdir()) else "warn",
                   name, str(path))


# ---------------------------------------------------------- Docker full auto
AUTO_ROOT = "/opt/skmr-installer"
AUTO_PLAN = "/etc/skmr/auto-install.json"
AUTO_NETWORK = "skmr-labs"


def prompt_install_method() -> str:
    say(f"\n    {NEON_GREEN}Full auto installer  (A){RESET}")
    say(f"    {DIM}-------------------{RESET}")
    say("    Create two Docker environments and automatically install and configure")
    say("    everything, including Claude integrations, AgentComm, and all AgentComm")
    say("    connection settings.")
    note("NOTE: The only manual steps required are entering the Docker containers")
    note("and logging in to Claude, assigning the agent names, obtaining the HackerOne")
    note("API credentials, and manually specifying the Obsidian vault path.")
    say(f"\n    {NEON_ORANGE}Manuel installer    (B){RESET}")
    say(f"    {DIM}-------------------{RESET}")
    say("    Connection settings, AgentComm, configurations, and similar components")
    say("    must all be configured manually.")
    note("If the system is intended to run outside Docker on your main terminal,")
    note("while the other agent worker operates on a remote machine, we recommend")
    note("using the manual installation method.")
    while True:
        value = ask("Which installation method would you like to use? [A/B]").casefold()
        if value in ("a", "b"):
            return value
        problem("Choose A or B.")


def prompt_auto_agents() -> list[dict]:
    agents = []
    for number in (1, 2):
        step(f"Agent {number}")
        name = ask(f"Agent {number} name")
        while any(a["name"].casefold() == name.casefold() for a in agents):
            problem("The two agent names must differ.")
            name = ask(f"Agent {number} name")
        # Display names remain intact. Linux/Samba account names must be safe
        # even when a display name contains Unicode, spaces or punctuation.
        normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
        identity = re.sub(r"[^a-z0-9]+", "-", normalized.casefold()).strip("-")
        if not identity or not identity[0].isalpha():
            identity = f"agent{number}-" + identity
        identity = identity[:31].rstrip("-")
        if any(a["id"] == identity for a in agents):
            identity = identity[:28] + f"-{number}"
        rank = ask_choice(f"Agent {number} rank", ["commander", "lieutenant"],
                          "commander" if number == 1 else "lieutenant")
        while number == 2 and rank == "lieutenant" and agents[0]["rank"] == "Lieutenant":
            problem("Both agents cannot be Lieutenant. Agent 2 must be Commander.")
            rank = ask_choice("Agent 2 rank", ["commander", "lieutenant"], "commander")
        host_vault = ""
        if rank == "commander":
            while True:
                value = ask(f"{name}'s LOCAL Obsidian vault path").strip('"')
                path = Path(value).expanduser().resolve()
                if path.is_dir() and (path / ".obsidian").is_dir():
                    host_vault = str(path)
                    break
                problem("Choose an existing Obsidian vault containing the .obsidian directory.")
        agents.append({"name": name, "id": identity, "rank": rank.capitalize(),
                       "host_vault": host_vault})
        note(f"Display name: {name}; AgentComm / Samba id: {identity}")
    commander = next(a for a in agents if a["rank"] == "Commander")
    for agent in agents:
        kali = agent is commander
        agent.update(container="kali-lab" if kali else "ubuntu-lab",
                     image="kalilinux/kali-rolling:latest" if kali else "ubuntu:24.04",
                     transport_host=kali)
        owner = agent if agent["rank"] == "Commander" else commander
        agent["vault_name"] = f"{owner['id']}-vault"
        agent["vault_local"] = "/vault" if agent["rank"] == "Commander" else f"/mnt/{owner['id']}-vault"
    if all(a["rank"] == "Commander" for a in agents):
        if agents[0]["host_vault"] == agents[1]["host_vault"]:
            raise ValueError("Two Commanders need distinct vault paths: each writes only its own vault.")
    return agents


def auto_answers(agent: dict, peer: dict) -> dict:
    commander = agent["rank"] == "Commander"
    peer_commander = peer["rank"] == "Commander"
    owner = agent if commander else peer
    grants = {}
    for item in (agent, peer):
        if item["rank"] == "Commander":
            grants[item["vault_name"]] = {agent["id"]: "write" if item is agent else "read",
                                           peer["id"]: "write" if item is peer else "read"}
    return {
        "SELF": agent["id"], "PEER": peer["id"], "RANK": agent["rank"], "PORT": "8080",
        "LOCAL_LAN": agent["ip"], "PEER_LAN": peer["ip"], "SMB_HOST": owner["ip"],
        "VAULT_NAME": agent["vault_name"], "TASKS_SHARE": f"{owner['id']}-tasks", "SMB_DOMAIN": owner["container"],
        "AGENTCOMM_DIR": "/root/agentcomm", "CLAUDE_DIR": "/root/.claude",
        "VAULT_LOCAL": agent["vault_local"], "VAULT_WINDOWS_PATH": owner["host_vault"],
        "API_BASE": "http://127.0.0.1:8080" if agent["transport_host"] else f"http://{peer['ip']}:8080",
        "VAULT_BACKING": "local" if commander else "smb",
        "SELF_ACCESS": "write" if commander else "read", "PEER_ACCESS": "read" if commander else "write",
        "SELF_HOST_ROLE": "host" if agent["transport_host"] else "consumer",
        "PEER_HOST_ROLE": "consumer" if agent["transport_host"] else "host",
        "STORAGE_HOST": "true" if commander else "false", "EXPORTS_VAULT": "true" if commander else "false",
        "VAULT_ORIGIN": "http://127.0.0.1:8080" if commander else f"http://{peer['ip']}:8080",
        "SMB_CREDFILE": (f"/root/agentcomm/{agent['id']}.smb.auth" if commander
                         else f"/etc/skmr/{peer['id']}.smb.auth"),
        "VAULT_PERMISSIONS": grants, "DISPLAY_NAME": agent["name"], "PEER_DISPLAY_NAME": peer["name"],
        "PEER_VAULT_LOCAL": f"/mnt/{peer['id']}-vault" if commander and peer_commander else "",
        "PEER_VAULT_NAME": peer["vault_name"] if commander and peer_commander else "",
    }


class AutoInstallError(RuntimeError):
    pass


def docker_prefix() -> list[str]:
    candidates = [["docker"]] if have("docker") else []
    for prefix in candidates:
        if run([*prefix, "info", "--format", "{{.OSType}}"], timeout=60).stdout.strip() == "linux":
            return prefix
    raise AutoInstallError("A running Linux Docker Engine is required. Run inside Linux/WSL with Docker access (sudo if needed), then retry.")


def docker_host_path(path: Path, prefix: list[str]) -> str:
    return str(path.resolve())


def docker_checked(prefix: list[str], argv: list[str], *, timeout: int = 1800,
                   display: bool = False, loading: bool = False) -> str:
    result = run([*prefix, *argv], timeout=timeout, loading=loading)
    if display and result.stdout:
        say(result.stdout.rstrip())
    if result.returncode != 0:
        # Never echo an argv that could include a credential; only Docker's error.
        detail = (result.stderr or result.stdout or "no output").strip()[-1200:]
        raise AutoInstallError(f"Docker operation failed (exit {result.returncode}): {detail}")
    return result.stdout.strip()


def install_claude(report: Report) -> None:
    step("Claude Code")
    executable = Path("/root/.local/bin/claude")
    if not executable.exists():
        result = run(["bash", "-o", "pipefail", "-c", "curl -fsSL https://claude.ai/install.sh | bash"], timeout=1800, loading=True)
        if result.returncode != 0:
            report.add("fail", "Claude Code", "native installer failed")
            return
    if executable.exists():
        link = Path("/usr/local/bin/claude")
        if not link.exists():
            link.symlink_to(executable)
    result = run([str(executable), "--version"], timeout=120)
    report.add("ok" if result.returncode == 0 and "Claude Code" in result.stdout else "fail",
               "Claude Code", result.stdout.strip() or "version check failed")


def check_auto_embedding(report: Report, endpoint: str = "http://127.0.0.1:11434") -> None:
    try:
        request = urllib.request.Request(endpoint.rstrip("/") + "/api/embed",
            data=json.dumps({"model": "bge-m3", "input": "SKMR installation check"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=180) as response:
            vectors = json.load(response).get("embeddings", [])
        if len(vectors) != 1 or not vectors[0] or not all(
                isinstance(v, (int, float)) and math.isfinite(v) for v in vectors[0]):
            raise ValueError("model returned no usable embedding")
        report.add("ok", "embedding inference", f"bge-m3 produced {len(vectors[0])} numeric dimensions")
    except Exception as error:
        report.add("fail", "embedding inference", f"bge-m3 failed: {error}")


def configure_auto_tasks(agentcomm_dir: Path, answers: dict, report: Report) -> None:
    """AgentComm's task allocator uses a distinct, writable SMB share."""
    if answers["STORAGE_HOST"] != "true":
        return
    folder = agentcomm_dir / "tasks"
    folder.mkdir(parents=True, exist_ok=True)
    start, end = "# >>> SKMR task share >>>", "# <<< SKMR task share <<<"
    block = "\n".join([start, f"[{answers['TASKS_SHARE']}]", f"   path = {folder}",
        "   browseable = no", "   read only = no", "   guest ok = no",
        f"   valid users = {answers['SELF']}", "   force user = root", "   force group = root",
        "   create mask = 0660", "   directory mask = 0770", end, ""])
    path = Path("/etc/samba/smb.conf")
    original = path.read_text(encoding="utf-8")
    if start in original and end in original:
        head, _, rest = original.partition(start)
        _, _, tail = rest.partition(end)
        updated = head + block + tail.lstrip("\n")
    else:
        updated = original.rstrip() + "\n\n" + block
    backup(path)
    atomic_write(path, updated)
    result = run(["testparm", "-s"], timeout=120)
    if result.returncode:
        report.add("fail", "task share", "Samba validation failed")
        return
    reload_result = run(["smbcontrol", "all", "reload-config"], timeout=120)
    probe = run(["smbclient", f"//127.0.0.1/{answers['TASKS_SHARE']}",
                 "-A", str(agentcomm_dir / f"{answers['SELF']}.smb.auth"), "-c", "ls"], timeout=120)
    report.add("ok" if reload_result.returncode == 0 and probe.returncode == 0 else "fail",
               "task share", f"[{answers['TASKS_SHARE']}] served separately from the read-only vault share")


def configure_auto_identity(claude_dir: Path, agentcomm_dir: Path, agent: dict,
                            peer: dict, answers: dict, report: Report, joined: dict) -> None:
    if joined and (not set(joined.get("tokens", {})) >= {agent["id"], peer["id"]}
                   or not joined.get("config_admin_token")):
        report.add("fail", "join secrets", "the join file must include both agent IDs and the administrative token")
        return
    write_agent_conf(agentcomm_dir, answers, report, False, joined)
    if report.failed:
        return
    reports_to = peer["id"] if agent["rank"] == "Lieutenant" else ""
    peer_reports = agent["id"] if peer["rank"] == "Lieutenant" else ""
    argv = [sys.executable, str(claude_dir / "skmr/cli.py"), "assign-role", "--non-interactive", "--local-only",
            "--vault", answers["VAULT_NAME"], "--local-access", answers["SELF_ACCESS"],
            "--remote-access", answers["PEER_ACCESS"], "--name", agent["id"], "--role", agent["rank"],
            "--host", agent["container"], "--remote-name", peer["id"], "--remote-role", peer["rank"],
            "--remote-host", peer["container"], "--remote-lan", peer["ip"],
            "--host-role", answers["SELF_HOST_ROLE"], "--remote-host-role", answers["PEER_HOST_ROLE"],
            "--reports-to", reports_to, "--remote-reports-to", peer_reports]
    result = run(argv, timeout=300, env=skmr_env(claude_dir, agentcomm_dir))
    report.add("ok" if result.returncode == 0 else "fail", "topology",
               f"{agent['id']} / {peer['id']}" if result.returncode == 0 else (result.stderr or result.stdout)[-400:])


def auto_mount(plan: dict) -> None:
    agent, peer = plan["agent"], plan["peer"]
    if peer["rank"] != "Commander":
        return
    destination = Path(f"/mnt/{peer['id']}-vault")
    credentials = Path(f"/etc/skmr/{peer['id']}.smb.auth")
    os.chmod(credentials, 0o600)
    destination.mkdir(parents=True, exist_ok=True)
    if not destination.is_mount():
        options = f"credentials={credentials},ro,vers=3.0,uid=0,gid=0,file_mode=0440,dir_mode=0550"
        result = run(["mount", "-t", "cifs", f"//{peer['container']}/{peer['vault_name']}",
                      str(destination), "-o", options], timeout=120)
        if result.returncode != 0:
            raise AutoInstallError(f"CIFS mount failed: {(result.stderr or result.stdout).strip()}")
    if not destination.is_mount():
        raise AutoInstallError(f"Mountpoint validation failed for {destination}")
    probe = run(["findmnt", "-n", "-o", "FSTYPE,OPTIONS", "--target", str(destination)], timeout=30)
    if probe.returncode or not probe.stdout.startswith("cifs ") or "ro" not in probe.stdout.split()[1].split(","):
        raise AutoInstallError(f"{destination} must be a read-only CIFS mount")
    list(destination.iterdir())  # A mountpoint without usable credentials is insufficient.


def install_auto_node(plan_path: Path, stage: str, join: str | None) -> int:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    agent, peer = plan["agent"], plan["peer"]
    claude_dir, agentcomm_dir = Path("/root/.claude"), Path("/root/agentcomm")
    report = Report()
    manager = detect_manager()
    if stage == "packages":
        install_system_packages(manager, report, False)
        install_cloudflared(manager, report, False)
        install_python_packages(report, False)
        install_claude(report)
        install_ollama(report, False, True)
        required = ("git", "curl", "mount.cifs", "sqlite3", "jq", "zstd", "ss", "cloudflared", "claude", "ollama")
        missing = [binary for binary in required if not have(binary)]
        model = run(["ollama", "list"], timeout=120)
        if missing or model.returncode or "bge-m3" not in model.stdout or not ollama_responding():
            report.add("fail", "package verification", "missing tools or embedding model: " + ", ".join(missing))
        else:
            report.add("ok", "package verification", "Claude, Ollama, bge-m3 and required tools verified")
            check_auto_embedding(report)
    elif stage == "configure":
        if agent["rank"] == "Lieutenant":
            auto_mount(plan)
        vault = Path(agent["vault_local"])
        if agent["rank"] == "Commander" and not (vault / ".obsidian").is_dir():
            raise AutoInstallError("Commander vault bind mount has no .obsidian marker")
        install_payload(claude_dir, agentcomm_dir, report, False)
        install_knowledge(claude_dir, report, False)
        answers = auto_answers(agent, peer)
        extra = (answers["PEER_VAULT_LOCAL"],) if answers["PEER_VAULT_LOCAL"] else ()
        install_settings(claude_dir, agentcomm_dir, vault, report, False, extra_directories=extra)
        install_config(claude_dir, vault, report, False)
        joined = load_join_secrets(Path(join) if join else None, report)
        if join and not joined:
            return 1
        configure_auto_identity(claude_dir, agentcomm_dir, agent, peer, answers, report, joined)
        if report.failed:
            say(report.render())
            return 1
        install_vault_system(vault, report, False, access=answers["SELF_ACCESS"])
        if report.failed:
            say(report.render())
            return 1
        credentials = plan.get("hackerone", {})
        if credentials:
            persist_environment(credentials, report, False)
        configure_samba(agentcomm_dir, vault, answers, manager, report, False, non_interactive=True)
        if agent["rank"] == "Commander" and not report.failed:
            configure_auto_tasks(agentcomm_dir, answers, report)
        install_instructions(claude_dir, vault, peer["id"], report, False)
        if answers["PEER_VAULT_LOCAL"]:
            instructions = claude_dir / "CLAUDE.md"
            with instructions.open("a", encoding="utf-8") as handle:
                handle.write(f"\nPeer vault: {answers['PEER_VAULT_LOCAL']} (READ ONLY). "
                             "Write only to your own vault; never write to the peer's mount.\n")
        materialise_skills(claude_dir, agentcomm_dir, report, False)
        # Both nodes expose the administrative API, while exactly one remains
        # the shared communication host. This makes peer role sync usable.
        start_agentcomm(agentcomm_dir, 8080, report, False, managed_restart=True)
        seed_review_queue(claude_dir, agentcomm_dir, report, False)
    elif stage == "activate":
        startup = ("#!/usr/bin/env python3\nimport sys\n"
                   f"sys.path.insert(0, {AUTO_ROOT!r})\nfrom install import auto_restart\n"
                   f"auto_restart({AUTO_PLAN!r})\n")
        atomic_write(Path("/usr/local/bin/skmr-lab-start"), startup, mode=0o755)
        report.add("ok", "restart wiring", "Samba, Ollama, CIFS mounts and AgentComm restart automatically")
    elif stage == "mount":
        auto_mount(plan)
        report.add("ok", "CIFS mount", f"/mnt/{peer['id']}-vault (read-only, listing verified)")
    elif stage == "verify":
        check(claude_dir, agentcomm_dir, report)
        verify(claude_dir, agentcomm_dir, Path(agent["vault_local"]), report, False,
               strict=True, initialise=agent["rank"] == "Commander")
    say(report.render())
    return 1 if report.failed else 0


def auto_restart(plan_path: str) -> None:
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    os.environ.update(plan.get("hackerone", {}))
    os.environ.update(skmr_env(Path("/root/.claude"), Path("/root/agentcomm")))
    if not start_ollama():
        raise AutoInstallError("Ollama could not restart")
    if plan["agent"]["rank"] == "Commander" and not start_smb():
        raise AutoInstallError("Samba could not restart")
    if plan["peer"]["rank"] == "Commander":
        for attempt in range(60):
            try:
                auto_mount(plan)
                break
            except (OSError, AutoInstallError):
                if attempt == 59:
                    raise
                time.sleep(2)
    report = Report()
    start_agentcomm(Path("/root/agentcomm"), 8080, report, False, managed_restart=True)
    if report.failed:
        raise AutoInstallError("AgentComm could not restart")
    os.execvp("sleep", ["sleep", "infinity"])


def transfer_auto_file(prefix: list[str], source: str, destination: str, local: Path) -> None:
    try:
        docker_checked(prefix, ["cp", source, docker_host_path(local, prefix)])
        os.chmod(local, 0o600)
        docker_checked(prefix, ["cp", docker_host_path(local, prefix), destination])
    finally:
        local.unlink(missing_ok=True)


def verify_auto_connections(prefix: list[str], agents: list[dict], report: Report) -> None:
    # Tokens are compared inside the containers, without ever returning them.
    digests = []
    for agent in agents:
        code = ("import hashlib,json;d=json.load(open('/root/agentcomm/agent.conf'));"
                "print(hashlib.sha256(json.dumps([d['tokens'],d['config_admin_token']],sort_keys=True).encode()).hexdigest())")
        digests.append(docker_checked(prefix, ["exec", agent["container"], "python3", "-c", code]))
        probe = """import json, urllib.request
d=json.load(open('/root/agentcomm/agent.conf'))
for base in (d['api_base'],d['peer_api_lan']):
    r=urllib.request.Request(base+'/api/status/',headers={'X-Agent-Token':d['token']})
    with urllib.request.urlopen(r,timeout=15) as response:
        assert response.status==200
print('Authenticated local/peer connections verified')
"""
        docker_checked(prefix, ["exec", agent["container"], "python3", "-c", probe], display=True)
    if len(set(digests)) != 1:
        raise AutoInstallError("AgentComm token pair or administrative token differs between the labs")
    report.add("ok", "AgentComm connections", "common token pair, admin token and authenticated routes verified")


def full_auto_install(report: Report, dry_run: bool) -> int:
    agents = prompt_auto_agents()
    step("HackerOne API credential")
    say(f"    Create a token here: {BOLD}{HACKERONE_TOKEN_URL}{RESET}")
    note("Enter the API identifier and API token. Leave empty to use the bundled dataset.")
    identifier = ask("HackerOne API identifier", allow_empty=True)
    token = ask("HackerOne API token", secret=True, allow_empty=True) if identifier else ""
    credentials = {"H1_API_IDENTIFIER": identifier, "H1_API_TOKEN": token} if identifier and token else {}
    report.add("ok" if credentials else "skip", "hackerone api",
               "credential will be stored in both labs" if credentials
               else "no credential given; bundled dataset used, API refresh disabled")
    ordered = sorted(agents, key=lambda a: a["container"] != "kali-lab")
    if dry_run:
        for agent in ordered:
            note(f"Would create {agent['container']} ({agent['image']}); {agent['name']} / {agent['rank']}")
            note(f"Vault: {agent['host_vault'] or agent['vault_local']}; peer mounts always READ ONLY")
            if agent["rank"] == "Commander":
                note(f"Would copy the complete bundled 00 System tree into {agent['host_vault']}/00 System")
        report.add("skip", "Docker full auto", "dry run; no files, containers, downloads or credentials written")
        say(report.render())
        return 0
    prefix = docker_prefix()
    for name in ("kali-lab", "ubuntu-lab"):
        result = run([*prefix, "container", "inspect", name], timeout=60)
        if result.returncode == 0:
            raise AutoInstallError(f"Container '{name}' already exists. It was left intact; use an empty Docker environment for full auto.")
    network = run([*prefix, "network", "inspect", AUTO_NETWORK], timeout=60)
    if network.returncode == 0:
        data = json.loads(network.stdout)[0]
        if data.get("Labels", {}).get("org.skmr.installer") != "auto":
            raise AutoInstallError(f"Network {AUTO_NETWORK} already exists and is not managed by this installer")
    else:
        docker_checked(prefix, ["network", "create", "--label", "org.skmr.installer=auto", AUTO_NETWORK])
    saved_plan = Path.home() / ".skmr" / "docker-install.json"
    backup(saved_plan)
    atomic_write(saved_plan, json.dumps({"agents": agents, "network": AUTO_NETWORK}, indent=2) + "\n", mode=0o600)
    report.add("ok", "preliminary configuration", f"{saved_plan}; API secrets are kept only in the labs")
    # A user-scoped temporary directory carries secrets; all host copies are
    # removed on success, Docker errors, interrupts and failed mounts alike.
    with tempfile.TemporaryDirectory(prefix="skmr-auto-") as temporary:
        folder = Path(temporary)
        os.chmod(folder, 0o700)
        for agent in ordered:
            step(f"Create {agent['container']} — {agent['name']} ({agent['rank']})")
            docker_checked(prefix, ["pull", agent["image"]], timeout=1800, loading=True)
            argv = ["run", "-d", "--name", agent["container"], "--hostname", agent["container"],
                    "--network", AUTO_NETWORK, "--label", "org.skmr.installer=auto", "--restart", "unless-stopped",
                    "--cap-add", "SYS_ADMIN", "--cap-add", "DAC_READ_SEARCH",
                    "--security-opt", "apparmor=unconfined"]
            if agent["host_vault"]:
                argv += ["--volume", docker_host_path(Path(agent["host_vault"]), prefix) + ":/vault"]
            argv += [agent["image"], "bash", "-c",
                     "while [ ! -x /usr/local/bin/skmr-lab-start ]; do sleep 2; done; exec /usr/local/bin/skmr-lab-start"]
            docker_checked(prefix, argv)
            details = json.loads(docker_checked(prefix, ["inspect", agent["container"]]))[0]
            agent["ip"] = details["NetworkSettings"]["Networks"][AUTO_NETWORK]["IPAddress"]
            note(f"Docker IP: {agent['ip']}; hostname: {agent['container']}")
            docker_checked(prefix, ["exec", "-e", "DEBIAN_FRONTEND=noninteractive", agent["container"],
                                   "bash", "-c", "apt-get update -qq && apt-get install -y -qq python3 curl ca-certificates"], timeout=1800, loading=True)
            docker_checked(prefix, ["exec", agent["container"], "mkdir", "-p", AUTO_ROOT, "/etc/skmr"])
            for source in (HERE / "install.py", PAYLOAD, KNOWLEDGE_SRC):
                docker_checked(prefix, ["cp", docker_host_path(source, prefix), f"{agent['container']}:{AUTO_ROOT}/"], timeout=600, loading=True)
            # Package provisioning needs no peer addresses. Configure only when
            # both labs exist and their actual Docker addresses are available.
            peer = next(a for a in agents if a is not agent)
            peer_plan = dict(peer, ip=peer.get("ip", ""))
            local = folder / "node.json"
            atomic_write(local, json.dumps({"agent": agent, "peer": peer_plan, "hackerone": credentials}) + "\n", mode=0o600)
            docker_checked(prefix, ["cp", docker_host_path(local, prefix), f"{agent['container']}:{AUTO_PLAN}"])
            docker_checked(prefix, ["exec", agent["container"], "chmod", "600", AUTO_PLAN])
            docker_checked(prefix, ["exec", "-e", "DEBIAN_FRONTEND=noninteractive", agent["container"], "python3",
                                   f"{AUTO_ROOT}/install.py", "--auto-node", AUTO_PLAN, "--auto-stage", "packages"], timeout=7200, display=True, loading=True)
            report.add("ok", agent["container"] + " packages", "installed and verified")
        # Rewrite complete plans before topology, so neither side gets a guessed IP.
        for agent in ordered:
            peer = next(a for a in agents if a is not agent)
            local = folder / "node.json"
            atomic_write(local, json.dumps({"agent": agent, "peer": peer, "hackerone": credentials}) + "\n", mode=0o600)
            docker_checked(prefix, ["cp", docker_host_path(local, prefix), f"{agent['container']}:{AUTO_PLAN}"])
            docker_checked(prefix, ["exec", agent["container"], "chmod", "600", AUTO_PLAN])
        first, second = ordered
        node_command = ["python3", f"{AUTO_ROOT}/install.py", "--auto-node", AUTO_PLAN]
        docker_checked(prefix, ["exec", "-e", "DEBIAN_FRONTEND=noninteractive", first["container"],
                               *node_command, "--auto-stage", "configure"], timeout=7200, display=True, loading=True)
        export = ("import json,os;d=json.load(open('/root/agentcomm/agent.conf'));"
                  "json.dump({'tokens':d['tokens'],'config_admin_token':d['config_admin_token']},open('/tmp/join.json','w'));"
                  "os.chmod('/tmp/join.json',0o600)")
        docker_checked(prefix, ["exec", first["container"], "python3", "-c", export])
        transfer_auto_file(prefix, f"{first['container']}:/tmp/join.json", f"{second['container']}:/tmp/join.json", folder / "join.json")
        transfer_auto_file(prefix, f"{first['container']}:/root/agentcomm/{first['id']}.smb.auth",
                           f"{second['container']}:/etc/skmr/{first['id']}.smb.auth", folder / "smb.auth")
        docker_checked(prefix, ["exec", second["container"], "chmod", "600", "/tmp/join.json", f"/etc/skmr/{first['id']}.smb.auth"])
        if second["rank"] == "Lieutenant":
            docker_checked(prefix, ["exec", second["container"], *node_command, "--auto-stage", "mount"], display=True, loading=True)
        docker_checked(prefix, ["exec", "-e", "DEBIAN_FRONTEND=noninteractive", second["container"],
                               *node_command, "--auto-stage", "configure", "--join", "/tmp/join.json"], timeout=7200, display=True, loading=True)
        if second["rank"] == "Commander":
            transfer_auto_file(prefix, f"{second['container']}:/root/agentcomm/{second['id']}.smb.auth",
                               f"{first['container']}:/etc/skmr/{second['id']}.smb.auth", folder / "smb.auth")
            for agent in ordered:
                docker_checked(prefix, ["exec", agent["container"], *node_command, "--auto-stage", "mount"], display=True, loading=True)
        for agent in ordered:
            docker_checked(prefix, ["exec", agent["container"], "rm", "-f", "/tmp/join.json"])
        verify_auto_connections(prefix, agents, report)
        for agent in ordered:
            docker_checked(prefix, ["exec", agent["container"], *node_command, "--auto-stage", "activate"], display=True)
        # Initialise/index the Commander first; the worker indexes the actual
        # exported PARA content without attempting to write through its RO mount.
        for agent in ordered:
            step(f"Verify {agent['container']}: installed files, full test suites and doctor")
            docker_checked(prefix, ["exec", agent["container"], *node_command, "--auto-stage", "verify"], timeout=7200, display=True, loading=True)
            report.add("ok", agent["container"] + " verification", "check, test suites and doctor passed")
    atomic_write(saved_plan, json.dumps({"agents": agents, "network": AUTO_NETWORK}, indent=2) + "\n", mode=0o600)
    say(f"\n{BOLD}Summary{RESET}\n{report.render()}")
    say(f"\n  {NEON_GREEN}Full automatic installation completed and verified.{RESET}")
    step("Enter the Docker labs and log in to Claude")
    for agent in ordered:
        command = subprocess.list2cmdline([*prefix, "exec", "-it", agent["container"], "bash", "-l"])
        say(f"    {BOLD}{agent['name']} ({agent['rank']}){RESET}: {command}")
        note(f"Inside the container: claude auth login; then cd {agent['vault_local']} && claude")
        note("In Claude: /skmr:doctor and /skmr:help")
    return 0


# ----------------------------------------------------------------------- main
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install the SKMR stack into Claude Code.")
    parser.add_argument("--claude-dir",
                        default=str(Path(os.path.expanduser("~")) / ".claude"),
                        help="Claude configuration directory (default: ~/.claude)")
    # Derived from the home directory rather than the literal /root/agentcomm:
    # under root that resolves to the same path, and an ordinary user gets a
    # directory they can actually write to.
    parser.add_argument("--agentcomm-dir",
                        default=None,
                        help="AgentComm installation directory (default: ~/agentcomm)")
    parser.add_argument("--extract-tokens", action="store_true",
                        help="export /root/agentcomm/agent.conf token fields to ./join.json (mode 0600); "
                             "use --agentcomm-dir to select another configuration")
    parser.add_argument("--dry-run", action="store_true",
                        help="show every action, change nothing")
    parser.add_argument("--method", choices=("A", "B", "a", "b"),
                        help="A: full Docker auto installer; B: existing manual installer")
    parser.add_argument("--auto-node", metavar="FILE", help=argparse.SUPPRESS)
    parser.add_argument("--auto-stage", choices=("packages", "configure", "mount", "activate", "verify"),
                        default="configure", help=argparse.SUPPRESS)
    parser.add_argument("--check", action="store_true",
                        help="verify an existing installation")
    parser.add_argument("--uninstall", action="store_true",
                        help="remove what this installer added")
    parser.add_argument("--no-packages", action="store_true",
                        help="skip system and python packages")
    parser.add_argument("--no-ollama", action="store_true",
                        help="skip the embedding backend")
    parser.add_argument("--no-prompts", action="store_true",
                        help="install files and wiring only; ask nothing")
    parser.add_argument("--rotate-secrets", action="store_true",
                        help="generate new transport and SMB secrets instead of keeping "
                             "the existing ones; the peer must then be re-joined")
    parser.add_argument("--join", "-j", metavar="FILE",
                        help="adopt the transport secrets of an already-installed peer "
                             "(its agent.conf, or a file carrying its 'tokens' object); "
                             "the two agents must share one token pair to authenticate")
    args = parser.parse_args()

    if platform.system() != "Linux" and not args.dry_run:
        problem("SKMR installation requires Linux. On Windows, run install.py inside WSL.")
        return 1

    if args.extract_tokens:
        source = Path(args.agentcomm_dir or "/root/agentcomm").expanduser() / "agent.conf"
        return extract_tokens(source, dry_run=args.dry_run)

    if args.auto_node:
        try:
            return install_auto_node(Path(args.auto_node), args.auto_stage, args.join)
        except (AutoInstallError, OSError, ValueError) as error:
            problem(str(error))
            return 1

    for line in logo_lines():
        say(line)
    say("")
    say(f"    {BOLD}SKMR installer{RESET}  {DIM}permanent memory, agent topology and "
        f"security knowledge for Claude Code{RESET}")

    claude_dir = Path(args.claude_dir).expanduser().resolve()
    agentcomm_dir = Path(args.agentcomm_dir or Path.home() / "agentcomm").expanduser().resolve()
    report = Report()

    if not PAYLOAD.is_dir():
        problem(f"payload directory missing: {PAYLOAD}")
        return 1
    say(f"    {DIM}claude dir: {claude_dir}   agentcomm dir: {agentcomm_dir}{RESET}")
    if args.dry_run:
        say(f"    {NEON_ORANGE}dry run: nothing will be written{RESET}")

    if not (args.check or args.uninstall):
        method = args.method.casefold() if args.method else ("b" if args.no_prompts else prompt_install_method())
        if method == "a":
            if args.no_prompts or args.no_packages or args.no_ollama or args.join or args.rotate_secrets:
                problem("Full auto requires interactive names/vault paths and the complete package stack. "
                        "Use method B for --no-prompts, --no-packages, --no-ollama, --join or secret rotation.")
                return 2
            try:
                return full_auto_install(report, args.dry_run)
            except (AutoInstallError, OSError, ValueError) as error:
                report.add("fail", "Docker full auto", str(error))
                say(f"\n{BOLD}Summary{RESET}\n{report.render()}")
                problem("Installation is incomplete. Created labs remain available for diagnosis; no existing lab was deleted.")
                return 1

    if args.check:
        check(claude_dir, agentcomm_dir, report)
    elif args.uninstall:
        uninstall(claude_dir, agentcomm_dir, report, args.dry_run)
    else:
        # Root is needed for package installation and for writing outside the
        # user's own tree -- not for an installation that stays inside it. The
        # warning used to fire unconditionally and name /root paths that this
        # run never touches, which turns a correct setup into a scary prompt.
        if getattr(os, "geteuid", lambda: -1)() != 0 and not args.dry_run:
            unwritable = [str(d) for d in (claude_dir, agentcomm_dir)
                          if not os.access(_nearest_existing(d), os.W_OK)]
            if unwritable:
                problem("Not running as root, and these directories are not writable: "
                        + ", ".join(unwritable))
                if args.no_prompts or not ask_yes("Continue anyway?", False):
                    return 1
            elif not args.no_packages:
                problem("Not running as root: system packages cannot be installed.")
                note("Re-run with sudo, or pass --no-packages and install them yourself.")
                if args.no_prompts or not ask_yes("Continue without installing packages?", False):
                    return 1
                args.no_packages = True
        manager = detect_manager()
        if args.no_packages:
            report.add("skip", "packages", "--no-packages")
        else:
            install_system_packages(manager, report, args.dry_run)
            install_cloudflared(manager, report, args.dry_run)
            wants_ollama = not args.no_ollama and (
                args.no_prompts or
                ask_yes("Install ollama and pull bge-m3 for vector search?"))
            install_ollama(report, args.dry_run, wants_ollama)
            install_python_packages(report, args.dry_run)

        agentcomm_dir.mkdir(parents=True, exist_ok=True) if not args.dry_run else None
        install_payload(claude_dir, agentcomm_dir, report, args.dry_run)
        install_knowledge(claude_dir, report, args.dry_run)
        vault, peer, answers = None, None, {}
        joined = load_join_secrets(Path(args.join) if args.join else None, report)
        if args.join and not joined:
            return 1
        if args.no_prompts:
            report.add("skip", "obsidian vault", "--no-prompts")
            report.add("skip", "hackerone api", "--no-prompts")
            report.add("skip", "topology", "--no-prompts; run /skmr:assign-role")
        else:
            vault = prompt_vault(report, args.dry_run)
        # After the vault, because Claude must be told the vault is readable.
        install_settings(claude_dir, agentcomm_dir, vault, report, args.dry_run)
        # The config must be bound before anything runs the packaged code, or a
        # child process resolves the build host's paths instead of these.
        install_config(claude_dir, vault, report, args.dry_run)
        if not args.no_prompts:
            prompt_hackerone(report, args.dry_run)
            answers = prompt_identity(claude_dir, agentcomm_dir, vault, report,
                                      args.dry_run, joined, args.rotate_secrets)
            peer = answers.get("PEER")
            if not report.failed:
                install_vault_system(vault, report, args.dry_run,
                                     access=answers.get("SELF_ACCESS", ""))
            configure_samba(agentcomm_dir, vault, answers, manager, report,
                            args.dry_run, args.rotate_secrets)
        # After the identity, because the policy names the vault and the peer.
        install_instructions(claude_dir, vault, peer, report, args.dry_run)
        materialise_skills(claude_dir, agentcomm_dir, report, args.dry_run)
        # Only the transport host serves; a consumer addresses the peer's.
        if str(answers.get("SELF_HOST_ROLE")) == "host":
            start_agentcomm(agentcomm_dir, int(answers.get("PORT", 8080)),
                            report, args.dry_run)
        elif answers:
            report.add("skip", "agentcomm service",
                       f"consumer; it addresses {answers.get('PEER')}'s service")
        seed_review_queue(claude_dir, agentcomm_dir, report, args.dry_run)
        verify(claude_dir, agentcomm_dir, vault, report, args.dry_run)

    say("")
    say(f"{BOLD}Summary{RESET}")
    say(report.render())
    say("")
    if report.failed:
        say(f"  {NEON_RED}{len(report.failed)} step(s) failed. "
            f"The installation is not complete.{RESET}")
        for _, name, detail in report.failed:
            say(f"    - {name}: {detail}")
        return 1
    if report.skipped:
        say(f"  {NEON_ORANGE}{len(report.skipped)} step(s) skipped.{RESET}")
    if not (args.check or args.uninstall or args.dry_run):
        say(f"  {NEON_GREEN}Installed.{RESET} Start a new Claude Code session, then run "
            f"{BOLD}/skmr:doctor{RESET} and {BOLD}/skmr:help{RESET}.")
        note(f"Open a new shell, or source {shell_profile()}, to pick up the environment.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say(f"\n{NEON_ORANGE}Interrupted. Nothing further was changed.{RESET}")
        sys.exit(130)
    except NoMoreInput as unanswered:
        say(f"\n{NEON_RED}Input ended while this question was still open:{RESET}")
        say(f"    {unanswered}")
        say(f"    {DIM}Run the installer interactively, or use --no-prompts to install "
            f"the files and wiring only.{RESET}")
        sys.exit(2)
