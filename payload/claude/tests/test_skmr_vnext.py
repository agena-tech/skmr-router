#!/usr/bin/env python3
"""Deterministic SKMR vNext acceptance suite (tests A-U)."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# <claude_dir>/tests/<this file> -- resolved so the suite inspects its own
# installation. Hardcoded, an ordinary user's run read the root account's hooks
# and died on a bytecode-cache write it had no permission to make.
CLAUDE = Path(__file__).resolve().parents[1]
VAULT = Path("/mnt/eliza-vault")
BACKUP = CLAUDE / "backups/skmr-obsidian-vnext-20260910T102403Z"
WRITER_PATH = CLAUDE / "skills/skmr/scripts/obsidian_memory.py"

# The user-authorized backup retirement removed the old implementation files.
# These are the measured baselines captured by the earlier A-U run, retained so
# the regression suite can continue proving reduction without restoring legacy code.
HISTORICAL_T = {
    "claude_old_lines": 165,
    "claude_old_bytes": 16457,
    "prompt_old_security_bytes": 1449,
    "prompt_old_nonsecurity_bytes": 1449,
    "session_start_old_bytes": 5598,
    "failure_old_bytes": 677,
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    require(spec is not None and spec.loader is not None, f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WRITER = load_module("skmr_writer_test", WRITER_PATH)
VAULT = WRITER.VAULT
VAULT = WRITER.VAULT


def run_script(path: Path, event: dict, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    merged["SKMR_DISABLE_INBOX"] = "1"
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, str(path)],
        input=json.dumps(event),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=merged,
        check=False,
    )


def candidate(title: str, semantic_class: str = "methodology", **overrides):
    value = {
        "title": title,
        "semantic_class": semantic_class,
        "content": f"A durable reusable observation for {title} with deterministic evidence.",
        "links": ["Anchor"],
        "tags": ["test"],
        "automatic": True,
        "novel": True,
        "reusable": True,
        "verified": True,
        "evidence": [{"kind": "test", "locator": str(Path(__file__).resolve())}],
    }
    value.update(overrides)
    return value


def seed(store) -> None:
    first = candidate("Anchor", "reference", automatic=False, explicit_user_request=True, status="user-provided", links=["Index"])
    second = candidate("Index", "reference", automatic=False, explicit_user_request=True, status="user-provided", links=["Anchor"])
    for item in (first, second):
        relative, kind, area = store.validate_candidate(item)
        path = store.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(store.render(item, kind, area))


def hashes(root: Path) -> dict[str, str]:
    return {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.glob("*"))
        if path.is_file()
    }


def test_a():
    import skmr_agent_policy as policy
    from skmr_permissions import can_write
    writable=can_write()
    if not writable:
        try: WRITER.VaultStore().validate_root(writable=True)
        except WRITER.PolicyDenied: pass
        else: raise AssertionError('READ permanent write was allowed')
    if policy.vault_mount_usable():
        policy.check_root(VAULT)
        return {'SMB_read':True,'assigned_write':writable,'live_vault_mutations':0}
    result=subprocess.run(['/usr/bin/python3',str(WRITER_PATH),'validate'],capture_output=True,text=True,timeout=50)
    require(result.returncode==0,result.stderr)
    require(json.loads(result.stdout).get('ok'),result.stdout)
    return {'authenticated_canonical_read':True,'assigned_write':writable,'live_vault_mutations':0}


def test_b():
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as state:
        store = WRITER.VaultStore(Path(root), Path(state))
        cases = {
            "personal": "02 Areas/Personal",
            "bug-hunting-project": "01 Projects/Bug Hunting/Target Project",
            "methodology": "03 Resources/Security/Methodologies",
            "vulnerability": "03 Resources/Security/Vulnerabilities",
            "completed-project": "04 Archives",
        }
        output = {}
        for semantic, prefix in cases.items():
            item = {"semantic_class": semantic, "title": "Route Test", "project": "Target Project"}
            relative, _, _ = store.route(item)
            require(str(relative).startswith(prefix), f"bad route for {semantic}: {relative}")
            output[semantic] = str(relative)
        return output


def test_c():
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as state:
        store = WRITER.VaultStore(Path(root), Path(state))
        notes = [
            candidate("SKMR Wiki Test C", "reference", automatic=False, explicit_user_request=True, status="user-provided", links=["SKMR Wiki Test B"]),
            candidate("SKMR Wiki Test B", "reference", automatic=False, explicit_user_request=True, status="user-provided", links=["SKMR Wiki Test C"]),
            candidate("SKMR Wiki Test A", "reference", automatic=False, explicit_user_request=True, status="user-provided", links=["SKMR Wiki Test B"]),
        ]
        # Existing graph fixtures; new writer calls may reference only existing notes.
        for item in notes[:2]:
            relative, kind, area = store.validate_candidate(item)
            path = store.vault / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(store.render(item, kind, area))
        for item in notes:
            preview = store.preview(item)
            if 'token' in preview:
                store.commit(preview["token"])
        validation = store.validate_vault()
        require(validation["ok"], json.dumps(validation))
        return validation


def test_d():
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as state:
        store = WRITER.VaultStore(Path(root), Path(state)); seed(store)
        item = candidate("Canonical Duplicate Test")
        first = store.preview(item); committed = store.commit(first["token"])
        second = store.preview(item)
        require(committed["operation"] == "create" and second["operation"] == "unchanged", "duplicate not suppressed")
        names = [path.name for path in store.all_notes() if "Canonical Duplicate Test" in path.name]
        require(names == ["Canonical Duplicate Test.md"], str(names))
        return {"first": committed["operation"], "second": second["operation"], "files": names}


def test_e():
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as state:
        store = WRITER.VaultStore(Path(root), Path(state)); seed(store)
        allowed = store.preview(candidate("Gate Pass"))["operation"] == "create"
        denied = {}
        for field in ("novel", "reusable", "verified"):
            try:
                store.preview(candidate(f"Gate {field}", **{field: False}))
                denied[field] = False
            except WRITER.PolicyDenied:
                denied[field] = True
        require(allowed and all(denied.values()), f"gate result: {allowed}, {denied}")
        return {"all_true": "allowed", **{key: "denied" for key in denied}}


def test_f():
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as state:
        store = WRITER.VaultStore(Path(root), Path(state)); seed(store)
        item = candidate(
            "Explicit Personal Memory",
            "personal",
            automatic=False,
            explicit_user_request=True,
            novel=False,
            reusable=False,
            verified=False,
            status="user-provided",
        )
        preview = store.preview(item); result = store.commit(preview["token"])
        require("02 Areas/Personal" in result["target"] and "[[Anchor]]" in Path(result["target"]).read_text(), str(result))
        return {"operation": result["operation"], "target": result["target"]}


def test_g():
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as state:
        store = WRITER.VaultStore(Path(root), Path(state)); seed(store)
        relative, _, _ = store.route({"semantic_class": "personal", "title": "Working Style"})
        require(str(relative).startswith("02 Areas/Personal/") and "Security" not in str(relative), str(relative))
        return {"durable_personal_route": str(relative), "native_route": False}


def test_h():
    memory = CLAUDE / "projects/-root/memory"
    before = hashes(memory)
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as state:
        store = WRITER.VaultStore(Path(root), Path(state)); seed(store)
        preview = store.preview(candidate("Isolation Test")); store.commit(preview["token"])
    after = hashes(memory)
    require(before == after, "native memory changed during permanent save")
    active = [CLAUDE / "bin/security-consultation-guard.py", CLAUDE / "bin/security-report-queue.py", CLAUDE / "bin/security-kb-session-start.py", WRITER_PATH]
    direct = [str(path) for path in active if "_important_information" in path.read_text(encoding="utf-8") or "projects/-root/memory" in path.read_text(encoding="utf-8")]
    require(not direct, f"active native permanent-memory reference: {direct}")
    return {"native_files": len(before), "hashes_equal": True, "active_write_references": direct}


def test_i():
    hook = CLAUDE / "bin/security-workflow-reminder.py"
    with tempfile.TemporaryDirectory() as state:
        env = {"SKMR_STATE_DIR": state}
        shell = run_script(hook, {"session_id": "i1", "prompt": "How do I list files with ls?"}, env)
        recall = run_script(hook, {"session_id": "i2", "prompt": "What did we previously learn about our OAuth testing methodology?"}, env)
        source = run_script(hook, {"session_id": "i3", "prompt": "This visible source returns 42. What does it return?"}, env)
        require(shell.stdout == "" and source.stdout == "" and recall.stdout, "lazy Obsidian routing failed")
        return {"scope": "hook reminder output only; no end-to-end model invocation tested", "shell_reminder": False, "prior_learning_reminder": True, "source_sufficient_reminder": False}


def test_j():
    hooks = "\n".join((CLAUDE / f"bin/{name}").read_text() for name in ("security-workflow-reminder.py", "security-failure-reminder.py", "security-kb-session-start.py"))
    policy = (CLAUDE / "CLAUDE.md").read_text()
    require("search_reports.py" not in hooks and "Skill(hackerone-intelligence" not in hooks, "hook automatically invokes HackerOne")
    require("only when the user asks" in policy, "lazy HackerOne policy missing")
    return {"scope": "static side-effect and policy check; model invocation not tested", "explicit_policy": True, "state_classifier_only": True}


def test_k():
    workflow = (CLAUDE / "bin/security-workflow-reminder.py").read_text().casefold()
    skill = (CLAUDE / "skills/bugbountyskills/SKILL.md").read_text()
    require("/root/.claude/knowledge/bugbountyskills" not in workflow and "subprocess" not in workflow and "smallest relevant files" in skill and "wholesale" in skill, "lazy repository policy failed")
    return {"scope": "static side-effect and policy check; model invocation not tested", "automatic_hook_read": False, "state_classifier_only": True, "smallest_source_rule": True}


def test_l():
    result = run_script(CLAUDE / "bin/security-kb-session-start.py", {"source": "startup", "session_id": "test-l"}, {"SKMR_TEST_MODE": "1"})
    require(result.returncode == 0 and result.stdout, result.stderr)
    require("BugBountySkills" not in result.stdout and "bugskill-ai" not in result.stdout, "repository rows remain visible")
    require("summary" not in result.stdout.casefold() and "CLAUDE REVIEW REQUIRED" not in result.stdout, "report content/review injected")
    return {"bytes": len(result.stdout.encode()), "repo_rows_visible": False, "report_content_injected": False, "mandatory_review": False}


def test_m():
    hook = CLAUDE / "bin/security-workflow-reminder.py"
    with tempfile.TemporaryDirectory() as state:
        env = {"SKMR_STATE_DIR": state}
        basic = run_script(hook, {"session_id": "m1", "prompt": "What is OAuth?"}, env)
        hunting = run_script(hook, {"session_id": "m2", "prompt": "Review this authorized target for IDOR."}, env)
        rename = run_script(hook, {"session_id": "m3", "prompt": "Help me rename this text file."}, env)
        require(basic.stdout and hunting.stdout and rename.stdout == "", "security/non-security scope failed")
        return {"oauth_bytes": len(basic.stdout.encode()), "hunting_bytes": len(hunting.stdout.encode()), "non_security_bytes": 0}


def test_n():
    workflow = CLAUDE / "bin/security-workflow-reminder.py"
    failure = CLAUDE / "bin/security-failure-reminder.py"
    with tempfile.TemporaryDirectory() as state:
        env = {"SKMR_STATE_DIR": state}
        run_script(workflow, {"session_id": "n1", "prompt": "Help me rename this text file."}, env)
        unrelated = run_script(failure, {"session_id": "n1", "hook_event_name": "PostToolUseFailure"}, env)
        run_script(workflow, {"session_id": "n2", "prompt": "Test this authorized target for SSRF."}, env)
        relevant = run_script(failure, {"session_id": "n2", "hook_event_name": "PostToolUseFailure"}, env)
        require(unrelated.stdout == "" and relevant.stdout, "failure hook scope failed")
        return {"unrelated_bytes": 0, "security_bytes": len(relevant.stdout.encode())}


def test_o():
    hook = CLAUDE / "bin/security-pending-stop.py"
    with tempfile.TemporaryDirectory() as state:
        env = {"SKMR_STATE_DIR": state}
        valid = run_script(hook, {}, env)
        queue = Path(state) / "security-new-reports.json"
        queue.write_text("{broken", encoding="utf-8")
        corrupt = run_script(hook, {}, env)
        queue.write_text('{"reports": []}', encoding="utf-8")
        tx = Path(state) / "skmr-obsidian-transactions"; tx.mkdir(); (tx / "a.json").write_text("{}")
        pending = run_script(hook, {}, env)
        require(valid.stdout == "" and corrupt.stdout and pending.stdout, "stop integrity gate failed")
        require("HackerOne başlangıç kontrolü:" not in hook.read_text(), "exact phrase gate remains")
        return {"valid_stop": "allowed", "corrupt_queue": "blocked", "transaction": "blocked", "phrase_required": False}


def test_p():
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as state:
        store = WRITER.VaultStore(Path(root), Path(state)); seed(store)
        samples = [
            "Authorization: Bearer mocksecret123", "Bearer mocksecret123", "eyJabcdefghijk.eyJabcdefghijk.abcdefghijk",
            "password: hunter2", "api_key=mockvalue123", "Cookie: session=mockvalue123",
        ]
        blocked = 0
        for index, sample in enumerate(samples):
            try:
                store.preview(candidate(f"Secret {index}", content="Durable text\n" + sample))
            except WRITER.PolicyDenied:
                blocked += 1
        require(blocked == len(samples), f"blocked {blocked}/{len(samples)}")
        return {"blocked": blocked, "attempted": len(samples), "real_secrets_used": False}


def test_q():
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as outside:
        store = WRITER.VaultStore(Path(root), Path(state))
        denied = 0
        for path in (Path("../outside-vault.md"), Path(".obsidian/test.md"), Path("/tmp/outside.md")):
            try:
                store.safe_target(path)
            except WRITER.PolicyDenied:
                denied += 1
        (Path(root) / "escape").symlink_to(Path(outside), target_is_directory=True)
        try:
            store.safe_target(Path("escape/test.md"))
        except WRITER.PolicyDenied:
            denied += 1
        require(denied == 4, f"path denials {denied}/4")
        return {"denied": denied, "attempted": 4}


def test_r():
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as state:
        store = WRITER.VaultStore(Path(root), Path(state)); seed(store)
        item = candidate("Concurrent Test")
        first = store.preview(item); second = store.preview(item)
        store.commit(first["token"])
        try:
            store.commit(second["token"])
            conflict = False
        except WRITER.PolicyDenied:
            conflict = True
        text = Path(first["target"]).read_text(encoding="utf-8")
        require(conflict and text.endswith("\n") and "Concurrent Test" in text, "concurrency protection failed")
        return {"first_commit": "created", "second_commit": "conflict", "truncated": False}


def test_s():
    # The invariant is that no retired native security note survives. The count
    # of 22 is this host's migration history: a machine that never held legacy
    # notes has nothing to migrate and no report, which is a correct state, not
    # a failure. Where a report exists it must still be complete and validated.
    live_old = list((CLAUDE / "projects/-root/memory").glob("*_important_information.md"))
    require(not live_old, f"retired native notes remain: {live_old}")
    report_path = CLAUDE / "state/skmr-migration-report.json"
    if not report_path.exists():
        return {"migration_report": "absent; nothing was migrated on this host",
                "live_native_security_notes": 0}
    report = json.loads(report_path.read_text())
    covered = {item["source"] for item in report["files"]
               if item["source"].endswith("_important_information.md")}
    require(covered and report["validation"]["ok"],
            f"migration report incomplete: {len(covered)}")
    return {"old_security_notes": len(covered), "classified": len(covered),
            "live_native_security_notes": 0, "vault_validation": True}


def test_t():
    new_claude = CLAUDE / "CLAUDE.md"
    with tempfile.TemporaryDirectory() as state_dir:
        env = {"SKMR_STATE_DIR": state_dir}
        new_security = run_script(CLAUDE / "bin/security-workflow-reminder.py", {"session_id": "t1", "prompt": "Review this authorized target for IDOR."}, env)
        new_nonsecurity = run_script(CLAUDE / "bin/security-workflow-reminder.py", {"session_id": "t2", "prompt": "Help me rename this text file."}, env)
        run_script(CLAUDE / "bin/security-workflow-reminder.py", {"session_id": "t3", "prompt": "Test this authorized target for XSS."}, env)
        new_failure = run_script(CLAUDE / "bin/security-failure-reminder.py", {"session_id": "t3"}, env)
    new_startup = run_script(CLAUDE / "bin/security-kb-session-start.py", {"source": "startup", "session_id": "metrics"}, {"SKMR_TEST_MODE": "1"})
    metrics = {
        **HISTORICAL_T,
        "claude_new_lines": len(new_claude.read_text().splitlines()), "claude_new_bytes": len(new_claude.read_bytes()),
        "prompt_new_security_bytes": len(new_security.stdout.encode()), "prompt_new_nonsecurity_bytes": len(new_nonsecurity.stdout.encode()),
        "session_start_new_bytes": len(new_startup.stdout.encode()),
        "failure_new_security_bytes": len(new_failure.stdout.encode()), "failure_new_nonsecurity_bytes": 0,
    }
    require(metrics["claude_new_bytes"] < metrics["claude_old_bytes"] and metrics["prompt_new_security_bytes"] < metrics["prompt_old_security_bytes"] and metrics["prompt_new_nonsecurity_bytes"] == 0 and metrics["session_start_new_bytes"] < metrics["session_start_old_bytes"], json.dumps(metrics))
    return metrics


def test_u():
    python_files = sorted((CLAUDE / "bin").glob("security-*.py")) + [WRITER_PATH]
    for path in python_files:
        py_compile.compile(str(path), doraise=True)
    optional = {"settings.local.json",              # Claude Code local overrides
                "skmr-migration-report.json"}       # only on a host that migrated
    for path in (CLAUDE / "settings.json", CLAUDE / "settings.local.json",
                 CLAUDE / "SKMR_SETTINGS_FRAGMENT.json",
                 CLAUDE / "state/skmr-migration-report.json"):
        if path.name in optional and not path.exists():
            continue
        json.loads(path.read_text(encoding="utf-8"))
    settings = json.loads((CLAUDE / "settings.json").read_text())
    fragment = json.loads((CLAUDE / "SKMR_SETTINGS_FRAGMENT.json").read_text())
    # Every hook the fragment declares must be wired. Equality was the wrong
    # relation: it also demanded that the machine have no OTHER hooks, so any
    # installation that merged SKMR into an existing settings.json failed while
    # nothing had drifted. A subset still catches a dropped or altered hook.
    installed = {json.dumps(hook, sort_keys=True)
                 for groups in settings["hooks"].values()
                 for group in groups for hook in group["hooks"]}
    declared = {json.dumps(hook, sort_keys=True)
                for groups in fragment["hooks"].values()
                for group in groups for hook in group["hooks"]}
    require(declared <= installed, f"fragment hook drift: {sorted(declared - installed)[:2]}")
    require(settings["permissions"].get("additionalDirectories", []) ==
            fragment["permissions"].get("additionalDirectories", []),
            "fragment directory drift")
    targets = []
    for groups in settings["hooks"].values():
        for group in groups:
            for hook in group["hooks"]:
                targets.extend(arg for arg in hook.get("args", [])
                               if arg.startswith(str(CLAUDE)))
    require(all(Path(path).is_file() for path in targets), f"missing hook target: {targets}")
    active_text = "\n".join(path.read_text(encoding="utf-8") for path in python_files)
    require("_important_information.md" not in active_text and "HackerOne başlangıç kontrolü:" not in active_text, "dead native or exact-phrase reference")
    require(not list((CLAUDE / "state/skmr-obsidian-transactions").glob("*.json")), "unfinished writer transaction")
    result=subprocess.run(['/usr/bin/python3',str(WRITER_PATH),'validate'],capture_output=True,text=True,timeout=50)
    require(result.returncode==0,result.stderr)
    vault_validation = json.loads(result.stdout)
    require(vault_validation["ok"], json.dumps(vault_validation))
    return {"python_files": len(python_files), "json_files": 3 + int((CLAUDE / "settings.local.json").exists()), "hook_targets": len(targets), "fragment_synced": True, "vault": vault_validation}


TESTS = {
    "A": test_a, "B": test_b, "C": test_c,
    "D": test_d, "E": test_e, "F": test_f, "G": test_g,
    "H": test_h, "I": test_i, "J": test_j, "K": test_k,
    "L": test_l, "M": test_m, "N": test_n, "O": test_o,
    "P": test_p, "Q": test_q, "R": test_r, "S": test_s,
    "T": test_t, "U": test_u,
}


def main() -> int:
    results = {}
    failed = False
    for name, function in TESTS.items():
        try:
            results[name] = {"status": "PASS", "evidence": function()}
        except Exception as error:
            failed = True
            results[name] = {"status": "FAIL", "error": f"{type(error).__name__}: {error}"}
    report = {"suite": "SKMR vNext A-U", "results": results}
    path = CLAUDE / "state/skmr-vnext-test-report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
