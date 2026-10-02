#!/usr/bin/env python3
"""Refresh SKMR reference sources and emit compact, non-blocking startup status."""

from __future__ import annotations

import base64
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Resolve the tree from this hook's own location (<claude_dir>/bin/<this file>)
# rather than from a literal. Hardcoded, an installed copy wrote its state, its
# skill inventory and its downloaded dataset into the machine the package was
# built on: a probe install under a different prefix rewrote this host's files.
CLAUDE_DIR = Path(os.environ.get("SKMR_CLAUDE_DIR", str(Path(__file__).resolve().parent.parent)))
KNOWLEDGE_DIR = CLAUDE_DIR / "knowledge"
BUGBOUNTY_DIR = KNOWLEDGE_DIR / "BugBountySkills"
BUGSKILL_DIR = KNOWLEDGE_DIR / "bugskill-ai"
DATA_DIR = KNOWLEDGE_DIR / "bugskill-ai-data"
LIVE_DATASET = DATA_DIR / "hackerone_public_reports.json"
SKILLS_DIR = CLAUDE_DIR / "skills"
# Honour the same override the review queue honours, so the two always agree on
# where the lock and the queue live. Without it they diverge whenever the state
# directory is relocated, and the lock stops being shared.
STATE_DIR = Path(os.environ.get("SKMR_STATE_DIR", str(CLAUDE_DIR / "state")))
# The managed paths must belong to the account the agent runs as, so that no
# other user can plant or rewrite a repository, a queue or a checkpoint. Writing
# that as uid 0 encoded a second, unrelated assumption -- that the agent runs as
# root -- which made every one of these checks fail for an ordinary user and
# left the security knowledge uninstallable. Under root this is still uid 0.
OWNER_UID = os.geteuid()
PENDING_REPORTS = STATE_DIR / "security-new-reports.json"
LAST_UPDATE = STATE_DIR / "security-kb-last-update.json"
LOCK_FILE = STATE_DIR / "security-report-review.lock"
MANAGED_SKILLS_FILE = STATE_DIR / "security-managed-skills.json"
H1_CHECKPOINT = STATE_DIR / "security-h1-api-checkpoint.json"
sys.path.insert(0, os.environ.get('AGENTCOMM_LIB', str(Path(__file__).resolve().parent.parent / 'lib')))
from skmr_agent_policy import VAULT, ROLE, vault_available, vault_mount_usable
from skmr_permissions import permission

BUGBOUNTY_URL = "https://github.com/0xN0RMXL/BugBountySkills.git"
BUGSKILL_URL = "https://github.com/SecurityTalent/bugskill-ai.git"
H1_API_URL = "https://api.hackerone.com/v1/hackers/hacktivity"
GIT = "/usr/bin/git"
VERSION = "2.0"
H1_OVERLAP = timedelta(days=30)
H1_MAX_PAGES = 199

# Apply ANSI sequences only after width calculations so styling cannot affect
# the fixed-width logo or table layout. Normal text deliberately uses the
# terminal default foreground, matching Claude's own response text.
RESET = "\033[0m"
TURQUOISE = RESET
NEON_GREEN = "\033[1;38;2;57;255;20m"
NEON_ORANGE = "\033[1;38;2;255;145;0m"
NEON_RED = "\033[1;38;2;255;49;49m"

# Claude Code indents hook system messages by five columns. Reserve six columns
# so the right border never wraps in a standard 80-column terminal, while still
# using the user's full 78-column design in wider terminals.
TERMINAL_COLUMNS = shutil.get_terminal_size(fallback=(80, 24)).columns
TABLE_WIDTH = min(78, max(58, TERMINAL_COLUMNS - 6))
NAME_COLUMN = 22
STATE_COLUMN = 12
DETAIL_COLUMN = TABLE_WIDTH - NAME_COLUMN - STATE_COLUMN - 4
TABLE_INNER = TABLE_WIDTH - 2


def run(command: list[str], timeout: int = 90) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout.decode(errors="replace") if isinstance(error.stdout, bytes) else (error.stdout or "")
        stderr = error.stderr.decode(errors="replace") if isinstance(error.stderr, bytes) else (error.stderr or "")
        return subprocess.CompletedProcess(command, 124, stdout, stderr or f"{timeout} saniyede zaman aşımı")


# SKMR_PROFILE_HARDENING_V1: availability is not integrity. Run the canonical
# validator before reporting the vault READY.
def vault_integrity_status() -> tuple[bool, str]:
    if not vault_available():
        return False, "SMB unavailable"
    local = vault_mount_usable()
    # The same guarded client validates the canonical tree over authenticated
    # HTTP when the physical local mount cannot be used.
    writer = Path(os.environ.get('SKMR_WRITER', str(Path(__file__).resolve().parent.parent / 'skills/skmr/scripts/obsidian_memory.py')))
    result = run(["/usr/bin/python3", str(writer), "validate"], timeout=45)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "validate failed").strip().replace("\n", " ")[:220]
        return False, "available but validation failed: " + detail
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False, "available but validator returned invalid JSON"
    if not payload.get("ok"):
        problems = len(payload.get("errors", {})) + len(payload.get("duplicates", {})) + len(payload.get("broken_links", {}))
        return False, f"available but vault validation failed ({problems} structural problem(s))"
    mode = permission(ROLE).upper()
    transport = 'local mount' if local else 'SMB unavailable; authenticated canonical API'
    return True, f"available via {transport}; assigned {mode}; validate ok ({payload.get('notes', 0)} notes)"


def atomic_write_json(path: Path, payload: object, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(mode)
    os.replace(temporary, path)


def verify_repo(path: Path, expected_url: str) -> tuple[bool, str]:
    try:
        knowledge = KNOWLEDGE_DIR.resolve(strict=True)
        parent = path.parent.resolve(strict=True)
    except OSError as error:
        return False, f"yönetilen yol çözümlenemedi: {error}"
    if KNOWLEDGE_DIR.is_symlink() or parent != knowledge or path.is_symlink():
        return False, "yönetilen repo yolu beklenen gerçek dizin değil"
    if path.exists() and path.stat().st_uid != OWNER_UID:
        return False, "yönetilen repo bu ajanın kullanıcısına ait değil"
    git_dir = path / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir() or git_dir.stat().st_uid != OWNER_UID:
        return False, "git çalışma kopyası yok"
    remote = run([GIT, "-C", str(path), "remote", "get-url", "origin"], timeout=15)
    if remote.returncode != 0 or remote.stdout.strip() != expected_url:
        return False, "origin adresi beklenen kaynakla eşleşmiyor"
    head = run([GIT, "-C", str(path), "rev-parse", "--verify", "HEAD"], timeout=15)
    if head.returncode != 0:
        return False, "geçerli HEAD yok"
    dirty = run([GIT, "-C", str(path), "status", "--porcelain"], timeout=30)
    if dirty.returncode != 0 or dirty.stdout.strip():
        return False, "vendor çalışma kopyası temiz değil"
    return True, head.stdout.strip()


def update_repo(path: Path, url: str) -> tuple[bool, str]:
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    if KNOWLEDGE_DIR.is_symlink() or KNOWLEDGE_DIR.stat().st_uid != OWNER_UID:
        return False, "güncellenemedi: knowledge dizini bu ajanın kullanıcısına ait değil"
    try:
        path.parent.resolve(strict=True).relative_to(KNOWLEDGE_DIR.resolve(strict=True))
    except (OSError, ValueError):
        return False, "güncellenemedi: hedef beklenen knowledge dizini dışında"
    if path.is_symlink() or (path.exists() and path.stat().st_uid != OWNER_UID):
        return False, "güncellenemedi: hedef symlink veya başka bir kullanıcının sahipliğinde"
    if (path / ".git").is_dir():
        remote = run([GIT, "-C", str(path), "remote", "get-url", "origin"])
        actual_url = remote.stdout.strip()
        if remote.returncode != 0 or actual_url != url:
            return False, "güncellenemedi: origin adresi beklenen kaynakla eşleşmiyor"

        before_result = run([GIT, "-C", str(path), "rev-parse", "HEAD"])
        before = before_result.stdout.strip()
        fetch = run([GIT, "-C", str(path), "fetch", "--prune", "--quiet", "origin", "main"], timeout=90)
        if fetch.returncode != 0:
            detail = (fetch.stderr or fetch.stdout).strip().splitlines()
            return False, f"güncellenemedi: {(detail[-1] if detail else 'git fetch hatası')[:240]}"
        result = run([GIT, "-C", str(path), "merge", "--ff-only", "--quiet", "FETCH_HEAD"])
        if result.returncode == 0:
            verified, verification = verify_repo(path, url)
            if not verified:
                return False, f"güncelleme sonrası doğrulama başarısız: {verification}"
            after_result = run([GIT, "-C", str(path), "rev-parse", "HEAD"])
            after = after_result.stdout.strip()
            if before and after and before != after:
                return True, f"güncellendi ({before[:7]}→{after[:7]})"
            return True, f"zaten güncel ({after[:7] if after else 'HEAD'})"
        detail = (result.stderr or result.stdout).strip().splitlines()
        return False, f"güncellenemedi: {(detail[-1] if detail else 'git hatası')[:240]}"

    path.parent.mkdir(parents=True, exist_ok=True)
    result = run(
        [GIT, "clone", "--branch", "main", "--single-branch", "--depth", "1", url, str(path)],
        timeout=120,
    )
    if result.returncode == 0:
        verified, verification = verify_repo(path, url)
        if verified:
            return True, "kuruldu ve güncellendi"
        return False, f"kurulum sonrası doğrulama başarısız: {verification}"
    detail = (result.stderr or result.stdout).strip().splitlines()
    return False, f"kurulamadı: {(detail[-1] if detail else 'git hatası')[:240]}"


def load_payload(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"reports": []}
    except (OSError, json.JSONDecodeError):
        return {"reports": []}


def load_pending_payload() -> dict:
    """Load the review queue fail-closed; corruption must never mean zero reports."""
    if not PENDING_REPORTS.exists():
        return {"reports": []}
    if PENDING_REPORTS.is_symlink() or not PENDING_REPORTS.is_file():
        raise ValueError("HackerOne inceleme kuyruğu normal bir dosya değil")
    if PENDING_REPORTS.stat().st_uid != OWNER_UID:
        raise ValueError("HackerOne inceleme kuyruğu bu ajanın kullanıcısına ait değil")
    try:
        payload = json.loads(PENDING_REPORTS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"HackerOne inceleme kuyruğu okunamadı: {error}") from error
    reports = payload.get("reports") if isinstance(payload, dict) else None
    if not isinstance(reports, list) or any(not isinstance(item, dict) for item in reports):
        raise ValueError("HackerOne inceleme kuyruğunun reports alanı geçersiz")
    ids = [report_id(item) for item in reports]
    if any(not value for value in ids) or len(ids) != len(set(ids)):
        raise ValueError("HackerOne inceleme kuyruğunda boş veya yinelenen rapor kimliği var")
    return payload


def parse_api_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def report_disclosed_at(report: dict) -> datetime | None:
    attrs = report.get("attributes", {}) if isinstance(report, dict) else {}
    return parse_api_time(attrs.get("disclosed_at")) if isinstance(attrs, dict) else None


def load_h1_checkpoint(old_reports: list[dict], previous_success: object) -> tuple[set[str], datetime]:
    """Use an API-owned checkpoint so repository refreshes cannot mask disclosures."""
    previous = parse_api_time(previous_success)
    if H1_CHECKPOINT.exists():
        if H1_CHECKPOINT.is_symlink() or not H1_CHECKPOINT.is_file() or H1_CHECKPOINT.stat().st_uid != OWNER_UID:
            raise ValueError("HackerOne API checkpoint bu ajanın kullanıcısına ait güvenli bir dosya değil")
        try:
            payload = json.loads(H1_CHECKPOINT.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"HackerOne API checkpoint okunamadı: {error}") from error
        ids = payload.get("observed_ids") if isinstance(payload, dict) else None
        checkpoint_time = parse_api_time(payload.get("last_success_at")) if isinstance(payload, dict) else None
        if not isinstance(ids, list) or any(not isinstance(value, str) or not value for value in ids):
            raise ValueError("HackerOne API checkpoint kimlik listesi geçersiz")
        if len(ids) != len(set(ids)) or checkpoint_time is None:
            raise ValueError("HackerOne API checkpoint yinelenen kimlik veya geçersiz zaman içeriyor")
        return set(ids), checkpoint_time

    # On first installation, the existing local database is the accepted baseline.
    # Future repository refreshes never advance this API-owned identity set.
    checkpoint_time = previous or datetime.now(timezone.utc)
    observed = {report_id(report) for report in old_reports if report_id(report)}
    return observed, checkpoint_time


def save_h1_checkpoint(observed_ids: set[str], last_success_at: datetime) -> None:
    atomic_write_json(
        H1_CHECKPOINT,
        {
            "last_success_at": last_success_at.astimezone(timezone.utc).isoformat(),
            "overlap_days": H1_OVERLAP.days,
            "observed_ids": sorted(observed_ids),
        },
    )


def report_id(report: dict) -> str:
    value = report.get("id") if isinstance(report, dict) else None
    if value is not None:
        return str(value)
    attrs = report.get("attributes", {}) if isinstance(report, dict) else {}
    return str(attrs.get("url") or attrs.get("title") or "")


def merge_reports(base: list[dict], additions: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    order: list[str] = []
    for report in additions + base:
        key = report_id(report)
        if key and key not in merged:
            merged[key] = report
            order.append(key)
    return [merged[key] for key in order]


def save_dataset(
    reports: list[dict],
    *,
    repository_merged_at: str,
    api_last_success_at: str | None,
    api_status: str,
) -> None:
    payload = {
        "source": "HackerOne Hacktivity plus bugskill-ai repository snapshot",
        "filter": "disclosed:true",
        "total_collected": len(reports),
        "repository_merged_at": repository_merged_at,
        "api_last_success_at": api_last_success_at,
        "api_status": api_status,
        "reports": reports,
    }
    atomic_write_json(LIVE_DATASET, payload)


def fetch_hackerone(existing_ids: set[str], last_success_at: datetime | None = None) -> tuple[list[dict], str, bool, int]:
    """Returns (reports, status message, complete, pages_read).

    pages_read is returned structurally. It used to be recovered by running a
    regex over the human-readable Turkish status message, so rewording that
    message silently reset the count to 0.
    """
    identifier = os.environ.get("H1_API_IDENTIFIER", "")
    token = os.environ.get("H1_API_TOKEN", "")
    if not identifier or not token:
        return [], "zorunlu canlı API çalışmadı; H1_API_IDENTIFIER ve H1_API_TOKEN tanımlı değil", False, 0

    auth = base64.b64encode(f"{identifier}:{token}".encode()).decode("ascii")
    discovered: list[dict] = []
    seen = set(existing_ids)
    page = 1
    cutoff = (last_success_at or datetime.now(timezone.utc)) - H1_OVERLAP
    while page <= H1_MAX_PAGES:
        query = urllib.parse.urlencode(
            {
                "queryString": "disclosed:true",
                "page[number]": page,
                "page[size]": 50,
                "sort": "-disclosed_at",
            }
        )
        request = urllib.request.Request(
            f"{H1_API_URL}?{query}",
            headers={
                "Accept": "application/json",
                "Authorization": f"Basic {auth}",
                "User-Agent": "Claude-Security-Knowledge-Updater/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return [], f"HackerOne API kontrolü tamamlanamadı: HTTP {error.code}; kısmi sonuçlar uygulanmadı", False, 0
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            return [], f"HackerOne API kontrolü tamamlanamadı: {str(error)[:180]}; kısmi sonuçlar uygulanmadı", False, 0

        if not isinstance(payload, dict) or "data" not in payload or not isinstance(payload.get("data"), list):
            return [], "HackerOne API kontrolü tamamlanamadı: yanıtta geçerli data listesi yok", False, 0
        if payload.get("errors"):
            return [], "HackerOne API kontrolü tamamlanamadı: yanıt hata alanı içeriyor", False, 0
        page_reports = payload["data"]
        if any(not isinstance(report, dict) or not report_id(report) for report in page_reports):
            return [], "HackerOne API kontrolü tamamlanamadı: geçersiz rapor öğesi", False, 0
        if not page_reports:
            return discovered, f"canlı API kontrol edildi ({page} sayfa; {H1_OVERLAP.days} gün örtüşme)", True, page

        for report in page_reports:
            key = report_id(report)
            if key and key not in seen:
                seen.add(key)
                discovered.append(report)
        dates = [report_disclosed_at(report) for report in page_reports]
        if any(value is None for value in dates):
            return [], "HackerOne API kontrolü tamamlanamadı: disclosed_at eksik veya geçersiz", False, 0
        if min(dates) <= cutoff or len(page_reports) < 50:
            return discovered, f"canlı API kontrol edildi ({page} sayfa; {H1_OVERLAP.days} gün örtüşme)", True, page
        time.sleep(0.2)
        page += 1
    return [], f"HackerOne API kontrolü tamamlanamadı: {H1_MAX_PAGES} sayfalık API penceresinde güvenli zaman sınırına ulaşılamadı", False, H1_MAX_PAGES


def report_summary(report: dict) -> dict:
    attrs = report.get("attributes", {})
    rels = report.get("relationships", {})
    program = rels.get("program", {}).get("data", {}).get("attributes", {}).get("name", "")
    generated = rels.get("report_generated_content", {}).get("data", {}).get("attributes", {})
    return {
        "id": report_id(report),
        "title": attrs.get("title", ""),
        "severity": attrs.get("severity_rating", ""),
        "cwe": attrs.get("cwe", ""),
        "program": program,
        "url": attrs.get("url", ""),
        "disclosed_at": attrs.get("disclosed_at", ""),
        "summary": generated.get("hacktivity_summary", ""),
    }


def backfill_queued_summaries(dataset_reports: list[dict]) -> int:
    """Refill queue entries whose summary was empty when they were first queued.

    HackerOne publishes ``hacktivity_summary`` some time after disclosure, so a
    report queued on the day it went public is stored with an empty summary, and
    no later run ever rebuilds it: the live API check only returns the newest
    pages, so an older queued id is never in ``reports`` again. The queue then
    shows a permanently bodyless report although the merged dataset has since
    acquired its summary -- measured on this host as 25 of 43 queued entries.
    Only an empty field is filled, so an existing summary is never overwritten.
    """
    if not dataset_reports:
        return 0
    queued = load_pending_payload().get("reports", [])
    stale = [item for item in queued if item.get("id") and not item.get("summary")]
    if not stale:
        return 0
    available = {report_id(report): report for report in dataset_reports if report_id(report)}
    filled = 0
    for item in stale:
        report = available.get(str(item["id"]))
        summary = report_summary(report).get("summary", "") if report else ""
        if summary:
            item["summary"] = summary
            filled += 1
    if filled:
        atomic_write_json(PENDING_REPORTS, {
            "detected_at": datetime.now(timezone.utc).isoformat(),
            "reports": queued,
        })
    return filled


def queue_new_reports(reports: list[dict]) -> int:
    if not reports:
        return 0
    queued = load_pending_payload().get("reports", [])
    combined: dict[str, dict] = {str(item.get("id")): item for item in queued if item.get("id")}
    before = set(combined)
    for report in reports:
        item = report_summary(report)
        # A re-queued report may arrive before HackerOne has published its
        # summary; keep the one already on disk rather than blanking it.
        if not item.get("summary"):
            item["summary"] = combined.get(item["id"], {}).get("summary", "")
        combined[item["id"]] = item
    payload = {
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "reports": list(combined.values()),
    }
    atomic_write_json(PENDING_REPORTS, payload)
    return len(set(combined) - before)


def strip_risky_skill_frontmatter(text: str) -> tuple[str, list[str]]:
    """Remove upstream auto-execution/auto-grant fields from YAML frontmatter."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return text, []

    output = [lines[0]]
    skipping = False
    removed: list[str] = []
    risky_keys = {"hooks", "allowed-tools"}
    for line in lines[1:]:
        if skipping and (line.strip() == "---" or (line and not line[0].isspace() and ":" in line)):
            skipping = False
        if not skipping and line and not line[0].isspace() and ":" in line:
            key = line.split(":", 1)[0].strip()
            if key in risky_keys:
                skipping = True
                removed.append(key)
                continue
        if not skipping:
            output.append(line)
    return "".join(output), removed


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve())
        return True
    except ValueError:
        return False


def load_managed_skill_names() -> set[str]:
    payload = load_payload(MANAGED_SKILLS_FILE)
    return {str(name) for name in payload.get("skills", []) if isinstance(name, str)}


def validate_skill_source(source: Path) -> tuple[bool, str]:
    try:
        resolved = source.resolve(strict=True)
    except OSError as error:
        return False, f"kaynak çözümlenemedi: {error}"
    if source.is_symlink() or not resolved.is_dir() or not is_within(resolved, BUGSKILL_DIR):
        return False, "kaynak doğrulanmış bugskill-ai ağacında normal bir dizin değil"
    skill_file = source / "SKILL.md"
    if skill_file.is_symlink() or not skill_file.is_file():
        return False, "SKILL.md normal bir dosya değil"
    for item in source.rglob("*"):
        if item.is_symlink():
            return False, f"skill ağacında symlink yasak: {item.relative_to(source)}"
    return True, ""


def sync_sanitized_skill(name: str, source: Path, previously_managed: set[str]) -> tuple[bool, str]:
    """Create a reviewed active copy without automatic upstream execution grants."""
    target = SKILLS_DIR / name
    marker = target / ".security-kb-managed"
    if target.is_symlink():
        current_target = target.resolve(strict=False)
        if name not in previously_managed and not is_within(current_target, BUGSKILL_DIR):
            return False, f"{target} kullanıcıya ait bir bağlantı; dokunulmadı"
    elif target.exists() and (not target.is_dir() or not marker.exists()):
        return False, f"{target} mevcut ve bu kurulum tarafından yönetilmiyor; dokunulmadı"

    temporary = SKILLS_DIR / f".{name}.security-kb-tmp-{os.getpid()}"
    backup = SKILLS_DIR / f".{name}.security-kb-old-{os.getpid()}"
    if temporary.is_symlink() or temporary.is_file():
        temporary.unlink()
    elif temporary.is_dir():
        shutil.rmtree(temporary)
    shutil.copytree(source, temporary, symlinks=False)

    skill_file = temporary / "SKILL.md"
    normalized, removed = strip_risky_skill_frontmatter(skill_file.read_text(encoding="utf-8"))
    if re.search(r"(?m)^\s*!`", normalized):
        shutil.rmtree(temporary)
        return False, f"{name}: dinamik shell satırı bulundu; aktif kopya reddedildi"
    skill_file.write_text(normalized, encoding="utf-8")
    (temporary / ".security-kb-managed").write_text(
        "Generated from bugskill-ai; upstream hooks and automatic tool grants are removed.\n",
        encoding="utf-8",
    )

    moved_old = False
    try:
        if target.is_symlink():
            target.unlink()
        elif target.is_dir():
            target.rename(backup)
            moved_old = True
        temporary.replace(target)
        if moved_old:
            shutil.rmtree(backup)
    except Exception:
        if not target.exists() and moved_old and backup.exists():
            backup.rename(target)
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    if removed:
        return True, f"{name}: aktif kopyadan güvenlik alanları çıkarıldı ({', '.join(removed)})"
    return True, f"{name}: denetlenmiş aktif kopya yenilendi"


def sync_bugskill_skills() -> tuple[int, int, list[str], list[str], bool]:
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    candidates: dict[str, list[Path]] = {}
    roots = [
        BUGSKILL_DIR / "Awesome-Claude-Code-Agent-Skills",
        BUGSKILL_DIR / "Personal-Claude-Code-Agent-Skills",
    ]
    for root in roots:
        if not root.is_dir():
            continue
        for skill_file in root.rglob("SKILL.md"):
            source = skill_file.parent
            candidates.setdefault(source.name, []).append(source)

    duplicates = {name: paths for name, paths in candidates.items() if len(paths) > 1}
    source_errors: list[str] = []
    desired: dict[str, Path] = {}
    for name, paths in candidates.items():
        if len(paths) != 1:
            continue
        valid_source, detail = validate_skill_source(paths[0])
        if valid_source:
            desired[name] = paths[0]
        else:
            source_errors.append(f"Güvensiz skill kaynağı etkinleştirilmedi: {name}: {detail}")
    previously_managed = load_managed_skill_names()

    installed = 0
    notes: list[str] = []
    warnings: list[str] = source_errors + [
        f"Yinelenen skill adı etkinleştirilmedi: {name} ({len(paths)} kaynak)"
        for name, paths in sorted(duplicates.items())
    ]
    managed_now: set[str] = set()
    for name, source in desired.items():
        ok, detail = sync_sanitized_skill(name, source, previously_managed)
        if ok:
            installed += 1
            managed_now.add(name)
            if "güvenlik alanları çıkarıldı" in detail:
                notes.append(detail)
        else:
            warnings.append(detail)

    for name in previously_managed - set(desired):
        link = SKILLS_DIR / name
        marker = link / ".security-kb-managed"
        if link.is_symlink() and is_within(link.resolve(strict=False), BUGSKILL_DIR):
            link.unlink()
        elif link.is_dir() and marker.is_file():
            retired = CLAUDE_DIR / "retired-security-skills" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") / name
            retired.parent.mkdir(parents=True, exist_ok=True)
            link.rename(retired)
        else:
            warnings.append(f"Eski yönetilen skill otomatik silinmedi: {link}")

    atomic_write_json(
        MANAGED_SKILLS_FILE,
        {"repository": BUGSKILL_URL, "skills": sorted(managed_now), "updated_at": datetime.now(timezone.utc).isoformat()},
    )
    expected = len(candidates)
    valid = not duplicates and not source_errors and installed == expected
    return installed, expected, notes, warnings, valid


def short_head(path: Path) -> str:
    result = run([GIT, "-C", str(path), "rev-parse", "--short=7", "HEAD"], timeout=15)
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def unique_errors(status: dict) -> list[str]:
    errors: list[str] = []

    def add(message: object) -> None:
        text = str(message or "").strip()
        if text and text not in errors:
            errors.append(text)

    if not status.get("bugbounty_ok"):
        add(f"BugBountySkills: {status.get('bugbounty')}")
    if not status.get("bugskill_ok"):
        add(f"bugskill-ai: {status.get('bugskill')}")
    if not status.get("snapshot_saved"):
        add("HackerOne Reports: snapshot was not refreshed")
    if not status.get("skills_valid"):
        add(
            "Installed Skills: "
            f"{status.get('skills', 0)} / {status.get('expected_skills', 0)} verified"
        )
    if not status.get("vault_ok"):
        add(f"Obsidian Vault: {status.get('vault_status')}")
    for warning in status.get("warnings", []):
        add(warning)
    if not errors:
        add(status.get("failure"))
    return errors


def color_status(value: str) -> str:
    positive = {"READY", "UPDATED", "UP TO DATE", "COMPLETE", "CLEANED", "VERIFIED", "FOUND"}
    neutral = {"STANDBY", "NONE", "PENDING"}
    is_positive = value in positive or value.endswith(" SAVED")
    color = NEON_GREEN if is_positive else NEON_ORANGE if value in neutral else NEON_RED
    # Restore the terminal default after the highlighted status cell.
    return f"{color}{value:<10}{TURQUOISE}"


def logo_lines() -> list[str]:
    # The red prefix on each row is only the three-part Agena mark. The wordmark
    # and copyright suffixes use the same default foreground as Claude text.
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
    return [f"{NEON_RED}{mark}{TURQUOISE}{suffix}{RESET}" for mark, suffix in rows]


def fit_detail(value: str, width: int | None = None) -> str:
    if width is None:
        width = DETAIL_COLUMN - 2
    if len(value) <= width:
        return value
    return value[: width - 1] + "…"


def make_row(name: str, state: str, detail: str) -> str:
    detail_width = DETAIL_COLUMN - 2
    return f"{TURQUOISE}│ {name:<20} │ {color_status(state)} │ {fit_detail(detail):<{detail_width}} │{RESET}"


def display_model(
    status: dict,
    new_count: int,
) -> tuple[list[tuple[str, str, str]], list[str]]:
    errors = unique_errors(status)
    skmr_state = "READY" if status.get("ok") and not errors else "ERROR"
    skmr_detail = "Lazy routing; Obsidian permanent memory" if skmr_state == "READY" else "Core integrity incomplete"
    skills_complete = bool(status.get("skills_valid"))
    reports_state = "UPDATED" if status.get("snapshot_saved") else "ERROR"
    reports_detail = f"{int(status.get('reports_total', 0)):,} reports; metadata only"
    if status.get("api_complete"):
        pages = int(status.get("api_pages", 0))
        api_state = "COMPLETE"
        api_detail = f"{pages} page{'s' if pages != 1 else ''}; checkpoint refreshed"
    else:
        api_state = "STANDBY"
        api_detail = fit_detail(str(status.get("api_status") or "optional refresh unavailable"))
    pending = int(status.get("pending_reports", new_count))
    new_state = "PENDING" if pending else "NONE"
    new_detail = f"{pending} queued for lazy review" if pending else "No queued disclosures"
    rows = [
        ("SKMR", skmr_state, skmr_detail),
        ("Obsidian Vault", "READY" if status.get("vault_ok") else "ERROR", str(status.get("vault_status") or "unknown")),
        ("Installed Skills", "COMPLETE" if skills_complete else "INCOMPLETE", f"{status.get('skills', 0)} / {status.get('expected_skills', 0)} installed"),
        ("HackerOne Reports", reports_state, reports_detail),
        ("Live API Check", api_state, api_detail),
        ("New Reports", new_state, new_detail),
        ("BugBountyWorkflow", "VERIFIED" if skills_complete else "ERROR", "Sanitized reference skills"),
    ]
    return rows, errors


def render_table(
    status: dict,
    new_count: int = 0,
) -> tuple[str, list[tuple[str, str, str]], list[str]]:
    rows, errors = display_model(status, new_count)
    title = "SECURITY KNOWLEDGE MEMORY ROUTER — SKMR"
    if len(title) > TABLE_INNER:
        title = title[:TABLE_INNER]
    table = [
        TURQUOISE + "╭" + "─" * TABLE_INNER + "╮" + RESET,
        TURQUOISE + "│" + title.center(TABLE_INNER) + "│" + RESET,
        TURQUOISE
        + "├"
        + "─" * NAME_COLUMN
        + "┬"
        + "─" * STATE_COLUMN
        + "┬"
        + "─" * DETAIL_COLUMN
        + "┤"
        + RESET,
        *(make_row(*row) for row in rows),
        TURQUOISE
        + "├"
        + "─" * NAME_COLUMN
        + "┴"
        + "─" * STATE_COLUMN
        + "┴"
        + "─" * DETAIL_COLUMN
        + "┤"
        + RESET,
    ]
    error_text = f" ERRORS: {len(errors)}"
    version_text = f"VERSION: {VERSION} "
    middle = " " * (TABLE_INNER - len(error_text) - len(version_text))
    error_color = TURQUOISE if not errors else NEON_RED
    table.append(
        f"{TURQUOISE}│{error_color}{error_text}{TURQUOISE}{middle}{version_text}│{RESET}"
    )
    table.append(TURQUOISE + "╰" + "─" * TABLE_INNER + "╯" + RESET)
    return "\n".join(table), rows, errors


def emit_vnext(status: dict, new_count: int, pending_total: int) -> None:
    """Emit measured status without injecting report content or mandatory review."""
    table, rows, errors = render_table(status, new_count)
    visible = "\u200b\n\n" + "\n".join([*logo_lines(), "", *table.splitlines()])
    if errors:
        context = "SKMR startup degraded: " + " | ".join(errors[:4])
    elif pending_total:
        context = f"SKMR ready. {pending_total} public disclosures await review. At the first user prompt, use the report-offer hook metadata to announce them and ask: Yeni raporları incelememi ister misin? Wait for consent; no report content was loaded."
    else:
        context = "SKMR ready. Obsidian is permanent memory; retrieval remains lazy."
    print(
        json.dumps(
            {
                "systemMessage": visible,
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": context,
                    "reloadSkills": True,
                },
            },
            ensure_ascii=False,
        )
    )


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        event = {}
    if event.get("source") in {"clear", "compact"}:
        return
    if os.environ.get("SKMR_TEST_MODE") == "1":
        status = {
            "ok": True,
            "vault_ok": True,
            "vault_status": "available and writable",
            "bugbounty_ok": True,
            "bugskill_ok": True,
            "snapshot_saved": True,
            "reports_total": 100,
            "api_complete": True,
            "api_pages": 2,
            "skills_valid": True,
            "skills": 17,
            "expected_skills": 17,
            "pending_reports": 3,
            "warnings": [],
        }
        emit_vnext(status, 3, 3)
        return

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    session_id = str(event.get("session_id") or "")
    vault_ok, vault_status = vault_integrity_status()

    status: dict[str, object] = {
        "bugbounty": "güncelleme tamamlanmadı",
        "bugbounty_ok": False,
        "bugbounty_changed": False,
        "bugbounty_commit": "unknown",
        "bugskill": "güncelleme tamamlanmadı",
        "bugskill_ok": False,
        "bugskill_changed": False,
        "bugskill_commit": "unknown",
        "hackerone": "dataset güncellemesi tamamlanmadı",
        "snapshot_saved": False,
        "reports_total": 0,
        "api_configured": bool(os.environ.get("H1_API_IDENTIFIER") and os.environ.get("H1_API_TOKEN")),
        "api_credentials_partial": bool(os.environ.get("H1_API_IDENTIFIER"))
        != bool(os.environ.get("H1_API_TOKEN")),
        "api_complete": False,
        "api_pages": 0,
        "api_overlap_days": H1_OVERLAP.days,
        "api_status": "canlı API kontrolü çalıştırılmadı",
        "skills": 0,
        "expected_skills": 0,
        "skills_valid": False,
        "workflow_cleaned": False,
        "detected_reports": 0,
        "pending_reports": 0,
        "notes": [],
        "warnings": [],
        "ok": False,
        "order": ["BugBountySkills", "bugskill-ai", "HackerOne dataset", "skill sync"],
        "session_id": session_id,
        "source": str(event.get("source") or ""),
        "vault_ok": vault_ok,
        "vault_status": vault_status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    LOCK_FILE.touch(exist_ok=True)
    pending_count = 0
    pending_total = 0
    backfilled = 0
    try:
        with LOCK_FILE.open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)

            # Reuse successful maintenance for six hours, including resume/fork.
            cached = load_payload(LAST_UPDATE)
            stamp = parse_api_time(cached.get('updated_at'))
            if cached.get('ok') and stamp and timedelta(0) <= datetime.now(timezone.utc) - stamp < timedelta(hours=6):
                cached = dict(cached)
                cached['vault_ok'] = status['vault_ok']
                cached['vault_status'] = status['vault_status']
                cached['ok'] = bool(cached.get('ok') and cached['vault_ok'])
                pending_total = len(load_pending_payload().get('reports', []))
                cached['pending_reports'] = pending_total
                emit_vnext(cached, 0, pending_total)
                return

            ok_first, status_first = update_repo(BUGBOUNTY_DIR, BUGBOUNTY_URL)
            status["bugbounty"] = status_first
            status["bugbounty_ok"] = ok_first
            status["bugbounty_changed"] = bool(
                ok_first and (status_first.startswith("güncellendi") or status_first.startswith("kuruldu"))
            )
            status["bugbounty_commit"] = short_head(BUGBOUNTY_DIR) if ok_first else "unknown"

            ok_second, status_second = update_repo(BUGSKILL_DIR, BUGSKILL_URL)
            status["bugskill"] = status_second
            status["bugskill_ok"] = ok_second
            status["bugskill_changed"] = bool(
                ok_second and (status_second.startswith("güncellendi") or status_second.startswith("kuruldu"))
            )
            status["bugskill_commit"] = short_head(BUGSKILL_DIR) if ok_second else "unknown"
            verified_second, verification = verify_repo(BUGSKILL_DIR, BUGSKILL_URL)
            if not ok_second or not verified_second:
                raise RuntimeError(f"bugskill-ai etkinleştirilmedi: {verification if not verified_second else status_second}")

            old_payload = load_payload(LIVE_DATASET)
            old_reports = old_payload.get("reports", [])
            if not isinstance(old_reports, list):
                old_payload = {}
                old_reports = []
            had_live_dataset = LIVE_DATASET.exists() and bool(old_reports)
            old_ids = {report_id(report) for report in old_reports if report_id(report)}
            pending_before = load_pending_payload().get("reports", [])
            pending_ids = {report_id(report) for report in pending_before if report_id(report)}
            checkpoint_exists = H1_CHECKPOINT.exists()
            observed_ids, checkpoint_time = load_h1_checkpoint(
                old_reports,
                old_payload.get("api_last_success_at"),
            )
            if not checkpoint_exists:
                # Persist the pre-refresh baseline before any network or dataset
                # mutation, so a failed first API run cannot be masked next time.
                save_h1_checkpoint(observed_ids, checkpoint_time)

            snapshot_payload = load_payload(BUGSKILL_DIR / "hackerone_public_reports.json")
            snapshot_reports = snapshot_payload.get("reports", [])
            if not isinstance(snapshot_reports, list) or not snapshot_reports:
                raise ValueError("doğrulanmış bugskill-ai checkout içinde geçerli HackerOne rapor listesi yok")

            merged = merge_reports(old_reports, snapshot_reports)
            baseline_ids = {report_id(report) for report in merged if report_id(report)}
            # On normal startups, compare the live API against the database as it
            # existed before this run. This prevents a freshly updated repository
            # snapshot from hiding a genuinely new API disclosure. On first install,
            # use the repository snapshot as the bootstrap baseline so thousands of
            # historical reports are not misclassified as new.
            api_known_ids = (observed_ids | pending_ids) if had_live_dataset else baseline_ids
            api_reports, api_status, api_complete, api_pages = fetch_hackerone(api_known_ids, checkpoint_time)
            status["api_status"] = api_status
            status["api_complete"] = api_complete
            status["api_pages"] = api_pages
            if api_complete:
                merged = merge_reports(merged, api_reports)
                api_last_success_at = datetime.now(timezone.utc).isoformat()
            else:
                api_last_success_at = old_payload.get("api_last_success_at")

            new_reports = api_reports if api_complete else []

            # Queue first. If the process stops before the dataset commit, the next run
            # safely deduplicates the same IDs instead of silently losing review work.
            pending_count = queue_new_reports(new_reports)
            # Late-published summaries reach the merged dataset but never the queue,
            # because an older queued id is no longer in the API window.
            backfilled = backfill_queued_summaries(merged)
            repository_merged_at = datetime.now(timezone.utc).isoformat()
            save_dataset(
                merged,
                repository_merged_at=repository_merged_at,
                api_last_success_at=api_last_success_at,
                api_status=api_status,
            )
            if api_complete:
                save_h1_checkpoint(
                    api_known_ids | {report_id(report) for report in api_reports if report_id(report)},
                    datetime.now(timezone.utc),
                )
            status["snapshot_saved"] = True
            status["reports_total"] = len(merged)
            status["hackerone"] = f"{len(merged)} rapor; repo snapshot güncellendi; {api_status}"

            skill_count, expected_skills, notes, warnings, skills_valid = sync_bugskill_skills()
            status["skills"] = skill_count
            status["expected_skills"] = expected_skills
            status["notes"] = notes
            status["warnings"] = warnings
            status["skills_valid"] = skills_valid
            status["workflow_cleaned"] = any(
                note.startswith("BugBountyWorkflow:") and "çıkarıldı" in note for note in notes
            )
            status["ok"] = bool(ok_first and ok_second and verified_second and skills_valid and status["vault_ok"])
            if not status["ok"]:
                status["failure"] = "repository, skill, or Obsidian integrity validation failed"
            pending_total = len(load_pending_payload().get("reports", []))
    except Exception as error:
        status["ok"] = False
        status["failure"] = f"{type(error).__name__}: {str(error)[:300]}"
        status.setdefault("warnings", []).append(str(status["failure"]))
        try:
            pending_total = len(load_pending_payload().get("reports", []))
        except Exception:
            pending_total = 0

    status["detected_reports"] = pending_count
    status["pending_reports"] = pending_total
    status["backfilled_summaries"] = backfilled
    status["updated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_write_json(LAST_UPDATE, status)
    emit_vnext(status, pending_count, pending_total)


if __name__ == "__main__":
    from skmr_inbox import run_with_inbox
    run_with_inbox(main, "SessionStart")
