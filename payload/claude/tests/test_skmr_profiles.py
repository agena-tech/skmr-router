#!/usr/bin/env python3
"""User-profile permanent-memory pipeline regressions.

All vault writes use temporary vaults; no fixture contains a real secret.
Numbers in method names map to the task's required-test list.
"""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

BASE = Path("/root/.claude")


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


W = load("prof_writer", BASE / "skills/skmr/scripts/obsidian_memory.py")
G = load("prof_guard", BASE / "bin/security-consultation-guard.py")
POLICY = load("prof_policy", BASE / "lib/skmr_agent_policy.py")


def store():
    return W.VaultStore(Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp()))


def prof(username, section, title, content, **ov):
    candidate = dict(
        semantic_class="user-profile", profile_username=username, profile_section=section,
        title=title, content=content, automatic=True, novel=True, reusable=True,
        status="user-provided", provenance="user-direct", tags=["profile"],
    )
    candidate.update(ov)
    return candidate


@contextmanager
def granted_policy(actor, access):
    """Exercise real grant resolution without depending on the installed roster."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        vault = root / "vault"
        (vault / ".obsidian").mkdir(parents=True)
        peer = actor + "-peer"
        conf = root / "agent.conf"
        conf.write_text(json.dumps({
            "self": actor, "peer": peer, "token": "fixture-token",
            "vault_local": str(vault), "vault_name": "FixtureVault", "vault_backing": "local",
            "vault_permissions": {"FixtureVault": {actor: access, peer: "write" if access == "read" else "read"}},
            "vault_origin": "http://127.0.0.1:1",
        }), encoding="utf-8")
        with patch.dict(os.environ, {"AGENTCOMM_CONF": str(conf)}):
            yield load("fixture_profile_policy", BASE / "lib/skmr_agent_policy.py")


class ProfilePipeline(unittest.TestCase):
    def with_index(self, s, username, title):
        s.commit(s.preview(prof(username, "index", title, f"Profile index for {username}.", links=[]))["token"])

    # 1
    def test_profile_route_maps_correctly(self):
        s = store()
        rel, note_type, area = s.route(prof("Anezatra", "devices-technical", W.expected_profile_title("anezatra", "devices-technical"), "x"))
        self.assertEqual(str(rel), ("05_Profiles/anezatra/07_Devices_and_Technical_Environment.md".replace("05_Profiles", W.PROFILES_DIR)))
        self.assertEqual((note_type, area), ("user-profile", "profile"))

    # 2
    def test_unsafe_usernames_rejected(self):
        for bad in ["", "..", "/etc", "a/b", "a\\b", ".obsidian", ".hidden", "a\x00b", "a\x1bb", "-lead", "trail-"]:
            with self.subTest(bad=bad), self.assertRaises(W.PolicyDenied):
                W.canonical_username(bad)

    # 3
    def test_case_equivalent_usernames_share_one_profile(self):
        self.assertEqual(W.canonical_username("Anezatra"), "anezatra")
        self.assertEqual(W.canonical_username("ANEZATRA"), "anezatra")
        s = store()
        self.with_index(s, "Anezatra", "Anezatra — Profile")
        r = s.route(prof("ANEZATRA", "identity", "Anezatra — Identity", "x"))[0]
        self.assertEqual(str(r), ("05_Profiles/anezatra/01_Identity.md".replace("05_Profiles", W.PROFILES_DIR)))

    # 4
    def test_unsupported_section_rejected(self):
        with self.assertRaises(W.PolicyDenied):
            store().route(prof("x", "nonsense", "T", "x"))

    # 5 & 6
    def test_index_bootstraps_without_link_but_sections_still_need_one(self):
        s = store()
        result = s.commit(s.preview(prof("a", "index", "A — Profile", "Index.", links=[]))["token"])
        self.assertEqual(result["operation"], "create")
        self.assertNotIn("no wikilink", s.validate_note(Path(result["target"])))
        with self.assertRaises(W.PolicyDenied):  # a section note may NOT skip links
            s.preview(prof("a", "identity", "A — Identity", "x", links=[]))

    # 7 & 8 & 21
    def test_two_users_share_stem_without_collision_and_links_resolve(self):
        s = store()
        self.with_index(s, "anezatra", "Anezatra — Profile")
        self.with_index(s, "berke", "Berke — Profile")
        s.commit(s.preview(prof("anezatra", "identity", "Anezatra — Identity", "A id.", links=["Anezatra — Profile"]))["token"])
        s.commit(s.preview(prof("berke", "identity", "Berke — Identity", "B id.", links=["Berke — Profile"]))["token"])
        names = {str(p.relative_to(s.vault)) for p in s.all_notes()}
        self.assertIn(("05_Profiles/anezatra/01_Identity.md".replace("05_Profiles", W.PROFILES_DIR)), names)
        self.assertIn(("05_Profiles/berke/01_Identity.md".replace("05_Profiles", W.PROFILES_DIR)), names)
        # Once sections exist, the index must link to them. Keep the fixture
        # conformant with the writer's backlink and full-merge proof contract.
        for username, label in [("anezatra", "Anezatra"), ("berke", "Berke")]:
            index_path = s.vault / W.PROFILES_DIR / username / W.PROFILE_SECTIONS["index"]
            candidate = prof(username, "index", W.expected_profile_title(username, "index"),
                f"Profile index for {username}.", links=[f"{label} — Identity"],
                profile_full_merge=True, base_sha256=W.sha256_file(index_path))
            s.commit(s.preview(candidate)["token"])
        v = s.validate_vault()
        self.assertTrue(v["ok"], v)
        self.assertFalse(v["duplicates"])         # numbered stems are not duplicates
        self.assertFalse(v["broken_links"])       # canonical titles resolve

    # 9 & 11
    def test_user_provided_needs_no_evidence_but_needs_novel_reusable(self):
        s = store()
        self.with_index(s, "u", "U — Profile")
        ok = s.preview(prof("u", "identity", "U — Identity", "x", links=["U — Profile"]))
        self.assertEqual(ok["operation"], "create")  # no evidence supplied, still fine
        for field in ("novel", "reusable"):
            with self.subTest(field=field), self.assertRaises(W.PolicyDenied):
                s.preview(prof("u", "identity", "U — Identity", "x", links=["U — Profile"], **{field: False}))

    # 10
    def test_verified_status_still_requires_strong_evidence(self):
        s = store()
        self.with_index(s, "v", "V — Profile")
        with self.assertRaises(W.PolicyDenied):
            s.preview(prof("v", "identity", "V — Identity", "x", links=["V — Profile"],
                           status="verified", provenance="user-confirmed", verified=False))
        good = s.preview(prof("v", "identity", "V — Identity", "x", links=["V — Profile"],
                              status="verified", provenance="user-confirmed", verified=True,
                              evidence=[{"kind": "url", "locator": "https://example.com/proof-v"}]))
        self.assertEqual(good["operation"], "create")

    def test_provenance_is_validated(self):
        s = store()
        self.with_index(s, "p", "P — Profile")
        with self.assertRaises(W.PolicyDenied):  # unknown provenance
            s.preview(prof("p", "identity", "P — Identity", "x", links=["P — Profile"], provenance="made-up"))
        with self.assertRaises(W.PolicyDenied):  # user-provided needs a user provenance
            s.preview(prof("p", "identity", "P — Identity", "x", links=["P — Profile"], provenance="external-observed"))
        ok = s.preview(prof("p", "identity", "P — Identity", "x", links=["P — Profile"],
                            status="source-observed", provenance="sondra-relay"))
        self.assertEqual(ok["operation"], "create")

    # 12 & 13 & 26
    def test_secrets_rejected_even_on_explicit_request(self):
        s = store()
        self.with_index(s, "s", "S — Profile")
        # Placeholders only — never a real secret.
        secrets = ["password: MOCK_ONLY_12345", "Authorization: Bearer MOCKTOKEN12345",
                   "-----BEGIN PRIVATE KEY-----", "api_key=MOCKKEY123456"]
        for secret in secrets:
            candidate = prof("s", "identity", "S — Identity", "Detail\n" + secret, links=["S — Profile"])
            candidate.pop("automatic")
            candidate["explicit_user_request"] = True
            with self.subTest(secret=secret[:10]), self.assertRaises(W.PolicyDenied):
                s.preview(candidate)

    # 14  (the exact local pre-check Sondra's `send.py candidate` runs)
    def test_sondra_can_validate_a_profile_candidate(self):
        s = store()
        candidate = prof("anezatra", "devices-technical", "Anezatra — Devices and Technical Environment",
                         "- Laptop: 32GB RAM", links=["Anezatra — Profile"])
        rel, note_type, area = s.validate_candidate(candidate)
        self.assertEqual(note_type, "user-profile")
        self.assertEqual(candidate["status"], "user-provided")
        self.assertEqual(candidate["provenance"], "user-direct")

    # 15  (assigned grants, not historical agent names, decide write authority)
    def test_read_grant_cannot_write_permanent_memory(self):
        for actor in ("sondra", "eliza", "custom-agent"):
            with self.subTest(actor=actor), granted_policy(actor, "read") as policy:
                with self.assertRaisesRegex(PermissionError, "Assigned READ"):
                    policy.check_root(policy.VAULT, writable=True)

    def test_write_grant_is_allowed_for_any_agent_name(self):
        for actor in ("sondra", "eliza", "custom-agent"):
            with self.subTest(actor=actor), granted_policy(actor, "write") as policy:
                # Authority-disagreement behaviour has its own dedicated suite.
                with patch.object(policy, "authoritative_write_grant", return_value=None):
                    policy.check_root(policy.VAULT, writable=True)

    # 16 & 17
    def test_technical_candidates_and_routes_unchanged(self):
        s = store()
        self.assertEqual(str(s.route({"semantic_class": "methodology", "title": "OAuth"})[0]),
                         "03 Resources/Security/Methodologies/OAuth.md")
        self.assertEqual(str(s.route({"semantic_class": "personal", "title": "Style"})[0]),
                         "02 Areas/Personal/Style.md")
        # Anchor+Index graph so a normal automatic note can be validated.
        for name, other in [("Anchor", "Index"), ("Index", "Anchor")]:
            (s.vault / f"{name}.md").write_text(
                f'---\ntitle: {name}\ntype: reference\narea: reference\nstatus: user-provided\nupdated: 2026-09-10\n---\n# {name}\n\nBody for {name}.\n\n## Related\n- [[{other}]]\n')
        tech = dict(title="Tech", semantic_class="methodology", content="Reusable technical lesson.",
                    links=["Anchor"], automatic=True, novel=True, reusable=True, verified=True,
                    evidence=[{"kind": "url", "locator": "https://example.com/x"}])
        self.assertEqual(s.preview(tech)["operation"], "create")
        with self.assertRaises(W.PolicyDenied):  # verified requires evidence, unchanged
            s.preview(dict(tech, title="Tech2", evidence=[]))

    # 18
    def test_non_profile_canonical_identity_uses_stem_and_title(self):
        s = store()
        (s.vault / "OAuth.md").write_text(
            '---\ntitle: OAuth Testing\ntype: methodology\narea: security\nstatus: user-provided\nupdated: 2026-09-10\n---\n# OAuth Testing\n\nBody.\n\n## Related\n- [[OAuth Testing]]\n')
        index = s.canonical_index()
        self.assertIn(W.normalized("OAuth"), index)          # stem is an alias
        self.assertIn(W.normalized("OAuth Testing"), index)  # title is an alias

    # 19 & 20
    def test_scoped_vs_unscoped_search(self):
        s = store()
        self.with_index(s, "anezatra", "Anezatra — Profile")
        self.with_index(s, "berke", "Berke — Profile")
        s.commit(s.preview(prof("anezatra", "identity", "Anezatra — Identity", "identity a", links=["Anezatra — Profile"]))["token"])
        s.commit(s.preview(prof("berke", "identity", "Berke — Identity", "identity b", links=["Berke — Profile"]))["token"])
        scoped = s.search("identity", profile="anezatra")
        self.assertTrue(scoped["matches"])
        self.assertTrue(all(m["path"].startswith(("05_Profiles/anezatra/".replace("05_Profiles", W.PROFILES_DIR))) for m in scoped["matches"]))
        self.assertEqual(scoped["profile"], "anezatra")
        allv = s.search("identity")
        self.assertTrue(any("berke" in m["path"] for m in allv["matches"]))

    # 22 & 23  (writer renders full note; merge is the caller's, diff proves preservation)
    def test_update_preserves_unrelated_content_and_supersession_is_explicit(self):
        s = store()
        self.with_index(s, "m", "M — Profile")
        s.commit(s.preview(prof("m", "devices-technical", "M — Devices and Technical Environment",
                                "- Laptop: Dell\n- Phone: Pixel", links=["M — Profile"]))["token"])
        merged = prof("m", "devices-technical", "M — Devices and Technical Environment",
                      "- Laptop: Dell\n- Phone: Pixel\n- GPU: RTX 4090", links=["M — Profile"])
        device_path = s.vault / W.PROFILES_DIR / "m" / W.PROFILE_SECTIONS["devices-technical"]
        merged.update(profile_full_merge=True, base_sha256=W.sha256_file(device_path))
        preview = s.preview(merged)
        self.assertEqual(preview["operation"], "update")
        self.assertIn("RTX 4090", preview["diff"])
        s.commit(s.preview(merged)["token"])
        text = (s.vault / ("05_Profiles/m/07_Devices_and_Technical_Environment.md".replace("05_Profiles", W.PROFILES_DIR))).read_text()
        for kept in ("Dell", "Pixel", "RTX 4090"):
            self.assertIn(kept, text)
        # A correction that supersedes must not leave the old value present as current.
        corrected = prof("m", "locations-travel", "M — Locations and Travel",
                         "Current city: Ankara (was Istanbul until 2026, superseded).",
                         links=["M — Profile"], provenance="user-confirmed")
        s.commit(s.preview(corrected)["token"])
        loc = (s.vault / ("05_Profiles/m/10_Locations_and_Travel.md".replace("05_Profiles", W.PROFILES_DIR))).read_text()
        self.assertIn("Ankara", loc)
        self.assertIn("superseded", loc)

    # 24
    def test_direct_vault_write_denied_for_profile_path(self):
        event = {
            "hook_event_name": "PreToolUse", "tool_name": "Write",
            "tool_input": {"file_path": str(G.VAULT / W.PROFILES_DIR / "anezatra/01_Identity.md"),
                           "content": "x"},
        }
        decision = G.handle(event)
        self.assertEqual(decision["hookSpecificOutput"]["permissionDecision"], "deny")

    # 25
    def test_preview_commit_hash_and_concurrency(self):
        s = store()
        self.with_index(s, "c", "C — Profile")
        item = prof("c", "identity", "C — Identity", "id", links=["C — Profile"])
        first = s.preview(item)
        second = s.preview(item)
        committed = s.commit(first["token"])
        self.assertEqual(committed["operation"], "create")
        self.assertEqual(committed["sha256"], first["after_sha256"])
        with self.assertRaises(W.PolicyDenied):  # stale second preview must conflict
            s.commit(second["token"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
