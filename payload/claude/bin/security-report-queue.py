#!/usr/bin/env python3
"""Lazy, transaction-safe review queue for disclosed HackerOne reports."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_DIR = Path(os.environ.get("SKMR_STATE_DIR", "/root/.claude/state"))
try:
    _host_conf=json.loads(Path(os.environ.get("AGENTCOMM_CONF","/root/agentcomm/agent.conf")).read_text())
    VAULT=Path(_host_conf["vault_local"])
except (OSError,ValueError,KeyError):
    VAULT=Path(os.environ.get("SKMR_VAULT","/mnt/skmr-vault"))
QUEUE = STATE_DIR / "security-new-reports.json"
LOCK = STATE_DIR / "security-report-review.lock"
SNAPSHOTS = STATE_DIR / "security-report-review-snapshots"
DECISIONS = STATE_DIR / "security-report-review-decisions"
TRANSACTIONS = STATE_DIR / "security-report-review-transactions"
RESULTS = STATE_DIR / "security-report-review-results"
TOKEN = re.compile(r"[0-9a-f]{24}")
SNAPSHOT_TTL = timedelta(days=7)


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, path)


def load_regular(path: Path, label: str, missing: dict | None = None) -> dict:
    if not path.exists() and missing is not None:
        return missing
    if path.is_symlink() or not path.is_file():
        raise SystemExit(f"{label} is missing or unsafe: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"{label} is unreadable: {error}") from error
    if not isinstance(payload, dict):
        raise SystemExit(f"{label} must be a JSON object")
    return payload


def load_queue() -> dict:
    payload = load_regular(QUEUE, "queue", {"reports": []})
    reports = payload.get("reports")
    if not isinstance(reports, list) or any(not isinstance(item, dict) for item in reports):
        raise SystemExit("queue reports must be a list of objects")
    ids = [str(item.get("id") or "") for item in reports]
    if any(not value for value in ids) or len(ids) != len(set(ids)):
        raise SystemExit("queue has empty or duplicate report IDs")
    return payload


def parse_time(value: object) -> datetime:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise SystemExit(f"invalid timestamp: {value}") from error
    return result if result.tzinfo else result.replace(tzinfo=timezone.utc)


def prune_review_state() -> None:
    """Age out finished review state.

    TRANSACTIONS is deliberately never pruned: an unresolved transaction is the
    replay record that keeps review work from being lost, so deleting one by age
    could destroy real work. Results, which used to accumulate one file per
    review forever, are aged out on the same TTL as snapshots.
    """
    now = datetime.now(timezone.utc)
    if SNAPSHOTS.is_dir():
        for path in SNAPSHOTS.glob("*.json"):
            token = path.stem
            if not TOKEN.fullmatch(token) or (TRANSACTIONS / path.name).exists():
                continue
            try:
                created = parse_time(load_regular(path, "snapshot").get("created_at"))
            except SystemExit:
                continue
            if now - created > SNAPSHOT_TTL:
                path.unlink(missing_ok=True)
                (DECISIONS / path.name).unlink(missing_ok=True)
    if RESULTS.is_dir():
        for path in RESULTS.glob("*.json"):
            if not TOKEN.fullmatch(path.stem) or (TRANSACTIONS / path.name).exists():
                continue
            try:
                resolved = parse_time(load_regular(path, "result").get("resolved_at"))
            except SystemExit:
                try:
                    resolved = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                except OSError:
                    continue
            if now - resolved > SNAPSHOT_TTL:
                path.unlink(missing_ok=True)


def show(report_id: str | None = None) -> None:
    prune_review_state()
    reports = load_queue()["reports"]
    if report_id is not None:
        reports = [item for item in reports if str(item["id"]) == report_id]
        if not reports:raise SystemExit("report is not pending: " + report_id)
    token = secrets.token_hex(12)
    snapshot = {
        "token": token,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "ids": [str(item["id"]) for item in reports],
    }
    atomic_json(SNAPSHOTS / f"{token}.json", snapshot)
    print(json.dumps({
        "token": token,
        "count": len(reports),
        "reports": [
            {key: item.get(key) for key in ("id", "title", "severity", "cwe", "program", "url", "disclosed_at")}
            for item in reports
        ],
        "decision_file": str(DECISIONS / f"{token}.json"),
        "schema": {"token": token, "reports": [{"id": "...", "decision": "saved|skipped", "reason": "...", "obsidian_files": []}]},
    }, ensure_ascii=False, indent=2))


def validate_obsidian_file(raw: object, review_started: datetime, report_id: str | None = None) -> str:
    path = Path(str(raw or ""))
    try:
        root = VAULT.resolve(strict=True)
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as error:
        raise SystemExit(f"invalid Obsidian evidence note: {path}: {error}") from error
    if path.is_symlink() or ".obsidian" in resolved.relative_to(root).parts or not resolved.is_file():
        raise SystemExit(f"unsafe Obsidian evidence note: {path}")
    if datetime.fromtimestamp(resolved.stat().st_mtime, timezone.utc) < review_started:
        raise SystemExit(f"Obsidian note was not updated during this review: {path}")
    text = resolved.read_text(encoding="utf-8")
    if not text.startswith("---\n") or "[[" not in text or "updated:" not in text:
        raise SystemExit(f"Obsidian note lacks SKMR metadata or wikilinks: {path}")
    if report_id is not None:
        # Reuse the canonical writer's metadata parser; never downgrade its save gate.
        import importlib.util
        import sys
        module_name = 'skmr_report_review_writer'
        writer = sys.modules.get(module_name)
        if writer is None:
            spec = importlib.util.spec_from_file_location(module_name, '/root/.claude/skills/skmr/scripts/obsidian_memory.py')
            writer = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = writer
            spec.loader.exec_module(writer)
        if writer.note_frontmatter(text).get('status') != 'verified':
            raise SystemExit(f"saved report lesson is not verified: {path}")
        expected = 'https://hackerone.com/reports/' + report_id
        if expected not in writer.frontmatter_locators(text):
            raise SystemExit(f"saved lesson does not cite reviewed report {report_id}: {path}")
    return str(resolved)


def finish(token: str, transaction: dict) -> dict:
    ids = set(transaction["ids"])
    queue = load_queue()
    remaining = [item for item in queue["reports"] if str(item.get("id")) not in ids]
    result = dict(transaction["result"])
    result["remaining_count"] = len(remaining)
    atomic_json(RESULTS / f"{token}.json", result)
    atomic_json(QUEUE, {"detected_at": queue.get("detected_at"), "updated_at": datetime.now(timezone.utc).isoformat(), "reports": remaining})
    (DECISIONS / f"{token}.json").unlink(missing_ok=True)
    (SNAPSHOTS / f"{token}.json").unlink(missing_ok=True)
    (TRANSACTIONS / f"{token}.json").unlink(missing_ok=True)
    return result


def resolve(token: str) -> None:
    if not TOKEN.fullmatch(token):
        raise SystemExit("invalid review token")
    transaction_path = TRANSACTIONS / f"{token}.json"
    if transaction_path.exists():
        print(json.dumps(finish(token, load_regular(transaction_path, "transaction")), ensure_ascii=False, indent=2))
        return
    snapshot = load_regular(SNAPSHOTS / f"{token}.json", "snapshot")
    ids = [str(value) for value in snapshot.get("ids", [])]
    decisions = [] if not ids else load_regular(DECISIONS / f"{token}.json", "decision file").get("reports")
    if not isinstance(decisions, list):
        raise SystemExit("decision reports must be a list")
    by_id = {str(item.get("id")): item for item in decisions if isinstance(item, dict)}
    if len(by_id) != len(decisions) or set(by_id) != set(ids):
        raise SystemExit("decisions must match snapshot IDs exactly once")
    review_started = parse_time(snapshot.get("created_at"))
    reviewed = []
    for report_id in ids:
        decision = by_id[report_id]
        verdict = str(decision.get("decision") or "").casefold()
        reason = " ".join(str(decision.get("reason") or "").split())
        if verdict not in {"saved", "skipped"} or not reason:
            raise SystemExit(f"{report_id}: decision and reason are required")
        files = decision.get("obsidian_files", [])
        if verdict == "saved":
            if not isinstance(files, list) or not files:
                raise SystemExit(f"{report_id}: saved requires obsidian_files")
            files = [validate_obsidian_file(value, review_started, report_id) for value in files]
        elif files:
            raise SystemExit(f"{report_id}: skipped cannot claim Obsidian files")
        reviewed.append({"id": report_id, "decision": verdict, "reason": reason, "obsidian_files": files})
    result = {
        "token": token,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
        "reviewed_count": len(reviewed),
        "learned_count": sum(item["decision"] == "saved" for item in reviewed),
        "skipped_count": sum(item["decision"] == "skipped" for item in reviewed),
        "reviewed": reviewed,
    }
    transaction = {"token": token, "ids": ids, "result": result}
    atomic_json(transaction_path, transaction)
    print(json.dumps(finish(token, transaction), ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("show", "status", "resolve", "result"))
    parser.add_argument("token", nargs="?")
    args = parser.parse_args()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOCK.touch(exist_ok=True)
    with LOCK.open("r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.action == "show":
            show(args.token)
        elif args.action == "status":
            print(len(load_queue()["reports"]))
        elif args.action == "resolve":
            if not args.token:
                raise SystemExit("resolve requires a token")
            resolve(args.token)
        else:
            if not args.token:
                raise SystemExit("result requires a token")
            print(json.dumps(load_regular(RESULTS / f"{args.token}.json", "result"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
