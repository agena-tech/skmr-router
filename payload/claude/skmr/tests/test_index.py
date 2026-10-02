#!/usr/bin/env python3
"""Incremental-index and version-invalidation test over a throwaway vault.

Runs against a temp directory so the real index is never touched. The live config
is snapshotted up front and restored in `finally`, including on failure.

    /usr/bin/python3 /root/.claude/skmr/tests/test_index.py
"""
import json
import os
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, "/root/.claude")

CONFIG = pathlib.Path(os.environ.get("SKMR_CONFIG", "/root/.claude/skmr/config/skmr.json"))
snapshot = CONFIG.read_text(encoding="utf-8")

tmp = pathlib.Path(tempfile.mkdtemp(prefix="skmr-vault-"))
idx = tmp / "idx.sqlite3"
(tmp / "a.md").write_text(
    "# Alpha\n\n## Servo\nThe shoulder joint uses a strong servo motor for lifting the arm.\n",
    encoding="utf-8")
(tmp / "b.md").write_text(
    "# Beta\n\n## Commands\nRun the send command to reach the peer. Error code 9876.\n",
    encoding="utf-8")

os.environ["SKMR_VAULT"] = str(tmp)
from skmr.core import config                          # noqa: E402
config.write({"vault_path": str(tmp), "index_path": str(idx)})
from skmr.memory.indexing import index                # noqa: E402
from skmr.memory.retrieval import embeddings, hybrid  # noqa: E402

failures: list[str] = []


def show(label, report):
    print(f"{label:<26} scanned={report.scanned} added={report.added} "
          f"modified={report.modified} deleted={report.deleted} chunks={report.chunks} "
          f"embedded={report.embedded} invalidated={report.invalidated}")
    return report


def expect(condition, message):
    if not condition:
        failures.append(message)
        print(f"   FAIL: {message}")


try:
    first = show("1) initial", index.sync())
    expect(first.added == 2, "initial sync should add 2 files")
    expect(first.embedded == first.chunks, "initial sync should embed every chunk")

    second = show("2) no change", index.sync())
    expect(second.added == second.modified == 0, "unchanged vault must not re-add")
    expect(second.chunks == 0 and second.embedded == 0,
           "unchanged files must not be re-chunked or re-embedded")

    (tmp / "a.md").write_text(
        "# Alpha\n\n## Servo\nThe shoulder joint uses a strong servo motor.\n\n"
        "## Extra\nNew section added here with enough text to stand as its own chunk.\n",
        encoding="utf-8")
    third = show("3) a.md modified", index.sync())
    expect(third.modified == 1 and third.added == 0, "modified file must count as modified")

    (tmp / "c.md").write_text("# Gamma\n\n## Note\nCVE-2026-1234 affects the parser badly.\n",
                              encoding="utf-8")
    fourth = show("4) c.md added", index.sync())
    expect(fourth.added == 1, "new file must be indexed")

    (tmp / "b.md").unlink()
    fifth = show("5) b.md deleted", index.sync())
    expect(fifth.deleted == 1, "deleted file must be removed from the index")

    print("\n-- deleted file must vanish from results --")
    out = hybrid.search("send command peer error", mode="bm25")
    stale = [r.relative for r in out.results if r.relative == "b.md"]
    print("   hits referencing b.md:", stale or "none (correct)")
    expect(not stale, "deleted file still appears in search results")

    print("\n-- embedding version bump must invalidate vectors --")
    config.write({"embedding_version": 2})
    config.load(refresh=True)
    embeddings.provider(refresh=True)
    sixth = show("6) embed version bumped", index.sync())
    expect(sixth.invalidated, "embedding version change must invalidate vectors")
    expect(sixth.embedded > 0, "invalidated vectors must be re-embedded")

    print("\n-- chunking version bump must rebuild everything --")
    config.write({"chunking_version": 2})
    config.load(refresh=True)
    seventh = show("7) chunking bumped", index.sync())
    expect(seventh.invalidated, "chunking version change must rebuild the index")
    expect(seventh.added == 2, "rebuild must re-add every remaining file")

    print("\n-- exact technical token still retrievable --")
    out = hybrid.search("CVE-2026-1234", mode="bm25")
    hits = [(r.relative, r.score) for r in out.results][:2]
    print("   ", hits)
    expect(any(relative == "c.md" for relative, _ in hits), "exact CVE token must match its note")

    stats = index.stats()
    print("\nfinal:", {k: stats[k] for k in ("files", "chunks", "vectors")})
finally:
    CONFIG.write_text(snapshot, encoding="utf-8")
    shutil.rmtree(tmp, ignore_errors=True)
    print("\nconfig restored from snapshot; temp vault removed")
    print("restored vault_path:", json.loads(snapshot)["vault_path"])

print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'PASS'}")
for failure in failures:
    print(f"  - {failure}")
sys.exit(1 if failures else 0)
