"""Installer contracts; no Docker daemon or personal vault is touched."""
import contextlib
import importlib.util
import io
import http.server
import inspect
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("installer", ROOT / "install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class AutoInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name) / "My Vault"
        (self.vault / ".obsidian").mkdir(parents=True)

    def prompt_agents(self, responses):
        with patch("builtins.input", side_effect=responses), contextlib.redirect_stdout(io.StringIO()):
            return installer.prompt_auto_agents()

    def test_lieutenant_first_installs_commander_on_kali_without_asking_worker_vault(self):
        agents = self.prompt_agents(["Endra", "lieutenant", "Elie", "commander", str(self.vault)])
        self.assertEqual(agents[0]["container"], "ubuntu-lab")
        self.assertEqual(agents[0]["vault_local"], "/mnt/elie-vault")
        self.assertEqual(agents[1]["container"], "kali-lab")
        self.assertEqual(agents[1]["host_vault"], str(self.vault.resolve()))

    def test_two_lieutenants_are_rejected_and_second_role_is_asked_again(self):
        agents = self.prompt_agents(["A", "lieutenant", "B", "lieutenant", "commander", str(self.vault)])
        self.assertEqual(agents[1]["rank"], "Commander")

    def test_two_commanders_keep_their_supplied_vaults(self):
        second = Path(self.temp.name) / "Other"
        (second / ".obsidian").mkdir(parents=True)
        agents = self.prompt_agents(["A", "commander", str(self.vault), "B", "commander", str(second)])
        self.assertEqual([a["host_vault"] for a in agents], [str(self.vault.resolve()), str(second.resolve())])
        self.assertEqual([a["vault_name"] for a in agents], ["a-vault", "b-vault"])
        agents[0]["ip"], agents[1]["ip"] = "172.30.0.2", "172.30.0.3"
        answers = installer.auto_answers(agents[0], agents[1])
        self.assertEqual(answers["VAULT_PERMISSIONS"], {
            "a-vault": {"a": "write", "b": "read"},
            "b-vault": {"a": "read", "b": "write"},
        })
        self.assertEqual(answers["PEER_VAULT_LOCAL"], "/mnt/b-vault")
        self.assertEqual(answers["PEER_ACCESS"], "read")

    def test_arbitrary_display_names_get_safe_distinct_samba_ids(self):
        agents = self.prompt_agents(["Elie / Bir", "commander", str(self.vault), "エンドラ", "lieutenant"])
        self.assertEqual(agents[0]["name"], "Elie / Bir")
        self.assertRegex(agents[0]["id"], r"^[a-z][a-z0-9-]{0,30}$")
        self.assertRegex(agents[1]["id"], r"^[a-z][a-z0-9-]{0,30}$")
        self.assertNotEqual(agents[0]["id"], agents[1]["id"])

    def test_worker_configuration_points_at_commander_and_shared_smb_credential(self):
        agents = self.prompt_agents(["Elie", "commander", str(self.vault), "Endra", "lieutenant"])
        agents[0]["ip"], agents[1]["ip"] = "172.30.0.2", "172.30.0.3"
        answers = installer.auto_answers(agents[1], agents[0])
        self.assertEqual(answers["API_BASE"], "http://172.30.0.2:8080")
        self.assertEqual(answers["VAULT_BACKING"], "smb")
        self.assertEqual(answers["SELF_ACCESS"], "read")
        self.assertEqual(answers["SMB_DOMAIN"], "kali-lab")
        report = installer.Report()
        destination = Path(self.temp.name) / "agentcomm"
        joined = {"tokens": {"elie": "c" * 64, "endra": "l" * 64}, "config_admin_token": "admin"}
        installer.write_agent_conf(destination, answers, report, False, joined)
        conf = json.loads((destination / "agent.conf").read_text())
        self.assertEqual(conf["tokens"], joined["tokens"])
        self.assertEqual(conf["config_admin_token"], "admin")
        self.assertEqual(conf["smb_credfile"], "/etc/skmr/elie.smb.auth")
        self.assertEqual(conf["vault_origin"], "http://172.30.0.2:8080")
        self.assertEqual(conf["smb_tasks_share"], "elie-tasks")

    def test_auto_doctor_rejects_process_failure_even_without_fail_rows(self):
        report = installer.Report()
        installer.record_doctor(report, 1, "", strict=True)
        self.assertTrue(report.failed)

    def test_auto_doctor_rejects_missing_output(self):
        report = installer.Report()
        installer.record_doctor(report, 0, "", strict=True)
        self.assertTrue(report.failed)

    def test_auto_doctor_rejects_failed_subsystem(self):
        report = installer.Report()
        installer.record_doctor(report, 0, "  [FAIL] vault unreachable\n", strict=True)
        self.assertTrue(report.failed)

    def test_auto_join_with_wrong_roster_is_rejected_before_config_is_written(self):
        agents = self.prompt_agents(["Elie", "commander", str(self.vault), "Endra", "lieutenant"])
        agents[0]["ip"], agents[1]["ip"] = "172.30.0.2", "172.30.0.3"
        report = installer.Report()
        destination = Path(self.temp.name) / "agentcomm"
        installer.configure_auto_identity(Path(self.temp.name) / ".claude", destination,
            agents[1], agents[0], installer.auto_answers(agents[1], agents[0]), report,
            {"tokens": {"wrong": "x", "names": "y"}, "config_admin_token": "admin"})
        self.assertTrue(report.failed)
        self.assertFalse((destination / "agent.conf").exists())

    def test_manual_noninteractive_dry_run_never_asks_for_docker_or_writes_files(self):
        claude = Path(self.temp.name) / ".claude"
        agentcomm = Path(self.temp.name) / "agentcomm"
        result = subprocess.run([sys.executable, "-B", str(ROOT / "install.py"),
            "--dry-run", "--no-packages", "--no-prompts", "--claude-dir", str(claude),
            "--agentcomm-dir", str(agentcomm)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Which installation method", result.stdout)
        self.assertFalse(claude.exists())
        self.assertFalse(agentcomm.exists())

    def test_full_auto_dry_run_shows_menu_and_makes_no_host_configuration(self):
        result = subprocess.run([sys.executable, "-B", str(ROOT / "install.py"), "--dry-run"],
            input=f"A\nElie\nCommander\n{self.vault}\nEndra\nLieutenant\n\n",
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Full auto installer", result.stdout)
        self.assertIn("Manuel installer", result.stdout)
        self.assertIn("ubuntu-lab", result.stdout)
        self.assertIn("no files, containers, downloads or credentials written", result.stdout)

    @unittest.skipUnless(shutil.which("bash"), "requires Linux Bash")
    def test_api_token_is_stored_literally_and_cannot_execute_shell_text(self):
        profile = Path(self.temp.name) / ".bashrc"
        marker = Path(self.temp.name) / "must-not-exist"
        token = f"literal-$HOME-$(touch {shlex.quote(str(marker))})"
        with patch.object(installer, "shell_profile", return_value=profile), patch.dict(os.environ):
            installer.persist_environment({"H1_API_TOKEN": token}, installer.Report(), False)
        result = subprocess.run(["bash", "-c", f"source {shlex.quote(str(profile))}; printf '%s' \"$H1_API_TOKEN\""],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.stdout, token)
        self.assertFalse(marker.exists())

    @unittest.skipUnless(sys.platform.startswith("linux"), "canonical role writer requires Linux")
    def test_canonical_role_writer_preserves_both_commanders_vault_permissions(self):
        other = Path(self.temp.name) / "Other"
        (other / ".obsidian").mkdir(parents=True)
        agents = self.prompt_agents(["Elie", "commander", str(self.vault), "Endra", "commander", str(other)])
        agents[0]["ip"], agents[1]["ip"] = "172.30.0.2", "172.30.0.3"
        claude = Path(self.temp.name) / ".claude"
        agentcomm = Path(self.temp.name) / "agentcomm"
        report = installer.Report()
        installer.install_payload(claude, agentcomm, report, False)
        installer.install_config(claude, self.vault, report, False)
        answers = installer.auto_answers(agents[0], agents[1])
        # Use isolated destinations, while exercising the shipped CLI role writer.
        answers.update(CLAUDE_DIR=str(claude), AGENTCOMM_DIR=str(agentcomm), VAULT_LOCAL=str(self.vault))
        installer.configure_auto_identity(claude, agentcomm, agents[0], agents[1], answers, report, {})
        self.assertFalse(report.failed, report.render())
        conf = json.loads((agentcomm / "agent.conf").read_text())
        self.assertEqual(conf["vault_permissions"], {
            "elie-vault": {"elie": "write", "endra": "read"},
            "endra-vault": {"elie": "read", "endra": "write"}})
        topology = json.loads((claude / "skmr/state/agents.json").read_text())
        self.assertEqual(topology["local"]["vault_permissions"], {"elie-vault": "write", "endra-vault": "read"})

    def embedding_probe(self, payload):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())
            def log_message(self, *_):
                pass
        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            report = installer.Report()
            installer.check_auto_embedding(report, f"http://127.0.0.1:{server.server_port}")
            return report
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_model_listing_is_insufficient_when_embedding_response_is_empty(self):
        report = self.embedding_probe({"model": "bge-m3", "embeddings": []})
        self.assertTrue(report.failed)

    def test_actual_numeric_embedding_response_is_verified(self):
        report = self.embedding_probe({"model": "bge-m3", "embeddings": [[0.1, 0.2, -0.3]]})
        self.assertFalse(report.failed)
        self.assertEqual(report.rows[0][0], "ok")

    def test_failed_ollama_restart_is_not_reported_as_a_healthy_lab(self):
        plan = Path(self.temp.name) / "auto-install.json"
        plan.write_text(json.dumps({"agent": {"rank": "Commander"}, "peer": {"rank": "Lieutenant"}}))
        with patch.object(installer, "start_ollama", return_value=False), \
             patch.object(installer, "start_smb", return_value=True), \
             patch.object(installer, "start_agentcomm"), patch.object(installer.os, "execvp"), patch.dict(os.environ):
            with self.assertRaisesRegex(installer.AutoInstallError, "Ollama"):
                installer.auto_restart(str(plan))

    @unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("git"), "requires Linux Git modes")
    def test_bundled_git_modes_are_restored_without_reverting_file_contents(self):
        repo = Path(self.temp.name) / "vendor"
        repo.mkdir()
        def git(*argv):
            result = subprocess.run(["git", "-C", str(repo), *argv], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout
        git("init", "--quiet")
        script, readme = repo / "tool.sh", repo / "README.md"
        script.write_text("#!/bin/sh\necho vendor\n")
        readme.write_text("original vendor documentation\n")
        script.chmod(0o755)
        readme.chmod(0o644)
        git("add", ".")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--quiet", "-m", "fixture")
        script.chmod(0o644)
        readme.chmod(0o755)
        installer.restore_vendor_modes(repo)
        self.assertEqual(git("status", "--porcelain"), "")
        self.assertTrue(script.stat().st_mode & 0o111)
        self.assertFalse(readme.stat().st_mode & 0o111)
        readme.write_text("local content must remain\n")
        installer.restore_vendor_modes(repo)
        self.assertEqual(readme.read_text(), "local content must remain\n")

    def test_doctor_banners_are_not_counted_as_subsystem_checks(self):
        report = installer.Report()
        installer.record_doctor(report, 0, "[SKMR]: Planning doctor\n" * 8, strict=True)
        self.assertTrue(report.failed)

    def test_peer_vault_directory_is_recorded_in_applied_claude_fragment(self):
        claude, agentcomm = Path(self.temp.name) / "claude", Path(self.temp.name) / "agentcomm"
        claude.mkdir()
        peer_mount = "/mnt/other-vault"
        installer.install_settings(claude, agentcomm, self.vault, installer.Report(), False,
                                   extra_directories=(peer_mount,))
        settings = json.loads((claude / "settings.json").read_text())
        fragment = json.loads((claude / "SKMR_SETTINGS_FRAGMENT.json").read_text())
        self.assertIn(peer_mount, settings["permissions"]["additionalDirectories"])
        self.assertEqual(settings["permissions"]["additionalDirectories"],
                         fragment["permissions"]["additionalDirectories"])

    def test_failed_service_manager_does_not_delay_direct_ollama_start(self):
        with patch.object(installer, "have", return_value=True), \
             patch.object(installer, "run", return_value=subprocess.CompletedProcess([], 1)), \
             patch.object(installer.subprocess, "Popen") as background, \
             patch.object(installer, "ollama_responding", side_effect=lambda: background.called), \
             patch.object(installer.time, "sleep") as sleep, \
             patch.object(Path, "open", side_effect=OSError):
            self.assertTrue(installer.start_ollama())
            sleep.assert_not_called()

    def test_auto_verification_fails_when_canonical_vault_validation_fails(self):
        claude = Path(self.temp.name) / "claude"
        writer = claude / "skills/skmr/scripts/obsidian_memory.py"
        writer.parent.mkdir(parents=True)
        writer.touch()
        report = installer.Report()
        with patch.object(installer, "run", return_value=subprocess.CompletedProcess([], 1, "", "invalid vault")):
            installer.verify(claude, Path(self.temp.name) / "agentcomm", self.vault,
                             report, False, strict=True)
        self.assertTrue(report.failed)

    def test_loading_cycles_in_place_and_preserves_command_results(self):
        self.assertIn("loading", inspect.signature(installer.run).parameters)
        class Terminal(io.StringIO):
            def isatty(self):
                return True
        terminal = Terminal()
        with contextlib.redirect_stdout(terminal):
            result = installer.run([sys.executable, "-c",
                "import sys,time; time.sleep(1.4); print('ready'); "
                "print('fixture error', file=sys.stderr); sys.exit(7)"], loading=True)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout.strip(), "ready")
        self.assertEqual(result.stderr.strip(), "fixture error")
        rendered = re.sub(r"\x1b\[[0-9;]*m", "", terminal.getvalue())
        self.assertEqual(re.findall(r"Loading, please wait (\.{1,3})", rendered)[:4],
                         [".", "..", "...", "."])
        self.assertNotIn("\n", rendered)
        self.assertTrue(rendered.endswith("\r"))
        self.assertFalse(any(t.name == "skmr-loading" for t in threading.enumerate()))

    def test_loading_timeout_stops_the_animation(self):
        self.assertIn("loading", inspect.signature(installer.run).parameters)
        class Terminal(io.StringIO):
            def isatty(self):
                return True
        terminal = Terminal()
        with contextlib.redirect_stdout(terminal):
            result = installer.run([sys.executable, "-c", "import time; time.sleep(5)"],
                                   timeout=0.2, loading=True)
        self.assertEqual(result.returncode, 127)
        self.assertIn("Loading, please wait", terminal.getvalue())
        self.assertTrue(terminal.getvalue().endswith("\r"))
        self.assertFalse(any(t.name == "skmr-loading" for t in threading.enumerate()))

    def test_loading_does_not_pollute_captured_output_or_normal_probes(self):
        self.assertIn("loading", inspect.signature(installer.run).parameters)
        terminal = io.StringIO()
        with contextlib.redirect_stdout(terminal):
            for options in ({"loading": True}, {}):
                result = installer.run([sys.executable, "-c", "print('ready')"], **options)
                self.assertEqual(result.stdout.strip(), "ready")
        self.assertEqual(terminal.getvalue(), "")

    def token_export(self, extra=()):
        source = Path(self.temp.name) / "agentcomm"
        source.mkdir(exist_ok=True)
        result = subprocess.run([sys.executable, "-B", str(ROOT / "install.py"),
            "--extract-tokens", "--agentcomm-dir", str(source), *extra],
            cwd=self.temp.name, capture_output=True, text=True, timeout=30)
        return result, Path(self.temp.name) / "join.json"

    @unittest.skipUnless(sys.platform.startswith("linux"), "token export requires Linux")
    def test_extract_tokens_exports_only_join_fields_and_restricts_existing_file(self):
        source = Path(self.temp.name) / "agentcomm"
        source.mkdir()
        payload = {"tokens": {"elie": "fixture-commander-secret", "endra": "fixture-worker-secret"},
                   "config_admin_token": "fixture-admin-secret", "unrelated": "must-not-be-exported"}
        (source / "agent.conf").write_text(json.dumps(payload))
        existing = Path(self.temp.name) / "join.json"
        existing.write_text("old export")
        existing.chmod(0o644)
        result, output = self.token_export()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(output.read_text()), {key: payload[key] for key in
                         ("tokens", "config_admin_token")})
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        for secret in (*payload["tokens"].values(), payload["config_admin_token"]):
            self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertNotIn("Which installation method", result.stdout)
        self.assertEqual((source / "agent.conf").read_text(), json.dumps(payload))

    @unittest.skipUnless(sys.platform.startswith("linux"), "token export requires Linux")
    def test_extract_tokens_missing_or_invalid_configuration_preserves_previous_export(self):
        source = Path(self.temp.name) / "agentcomm"
        source.mkdir()
        output = Path(self.temp.name) / "join.json"
        output.write_text("previous export")
        for payload in (None, {"tokens": {"elie": "fixture-secret", "endra": "other-fixture-secret"}}):
            with self.subTest(payload=payload is not None):
                if payload is not None:
                    (source / "agent.conf").write_text(json.dumps(payload))
                result, target = self.token_export()
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(target.read_text(), "previous export")
                self.assertNotIn("fixture-secret", result.stdout + result.stderr)
                self.assertFalse(list(Path(self.temp.name).glob(".join-*")))

    @unittest.skipUnless(sys.platform.startswith("linux"), "token export requires Linux")
    def test_extract_tokens_dry_run_creates_no_export(self):
        source = Path(self.temp.name) / "agentcomm"
        source.mkdir()
        (source / "agent.conf").write_text(json.dumps({
            "tokens": {"elie": "one", "endra": "two"}, "config_admin_token": "admin"}))
        result, output = self.token_export(("--dry-run",))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(output.exists())

    def test_extract_tokens_defaults_to_root_configuration_and_bypasses_installer(self):
        with patch.object(sys, "argv", ["install.py", "--extract-tokens"]), \
             patch.object(installer.platform, "system", return_value="Linux"), \
             patch.object(installer, "extract_tokens", create=True, return_value=0) as export, \
             patch("builtins.input", side_effect=AssertionError("export must not prompt")), \
             contextlib.redirect_stderr(io.StringIO()):
            try:
                code = installer.main()
            except SystemExit as error:
                code = error.code
        self.assertEqual(code, 0)
        export.assert_called_once_with(Path("/root/agentcomm/agent.conf"), dry_run=False)


class VaultSystemInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "package" / "00 System"
        (self.source / "SKMR" / "Empty folder").mkdir(parents=True)
        (self.source / "Home.md").write_text("# Home\n", encoding="utf-8")
        (self.source / "SKMR" / "SKMR.md").write_text("# SKMR — Türkçe\n", encoding="utf-8")
        (self.source / "SKMR" / ".asset").write_bytes(b"\x00\xff\x01")
        self.vault = self.root / "Selected Vault"
        (self.vault / ".obsidian").mkdir(parents=True)

    def copy(self, *, access="write", dry_run=False, vault=None):
        self.assertTrue(callable(getattr(installer, "install_vault_system", None)),
                        "installer must copy the bundled 00 System directory")
        report = installer.Report()
        with patch.object(installer, "VAULT_SYSTEM_SRC", self.source, create=True), \
             contextlib.redirect_stdout(io.StringIO()):
            installer.install_vault_system(self.vault if vault is None else vault,
                                           report, dry_run, access=access)
        return report

    def test_copies_every_file_and_empty_subdirectory_into_selected_vault(self):
        report = self.copy()
        self.assertFalse(report.failed, report.render())
        destination = self.vault / "00 System"
        for source in self.source.rglob("*"):
            target = destination / source.relative_to(self.source)
            if source.is_dir():
                self.assertTrue(target.is_dir(), str(target))
            else:
                self.assertEqual(target.read_bytes(), source.read_bytes())
        self.assertFalse((destination / "00 System").exists())

    def test_repeat_install_preserves_unrelated_notes_and_backs_up_replaced_files(self):
        self.copy()
        destination = self.vault / "00 System"
        home = destination / "Home.md"
        home.write_text("my earlier home", encoding="utf-8")
        extra = destination / "My note.md"
        extra.write_text("my extra note", encoding="utf-8")
        report = self.copy()
        self.assertFalse(report.failed, report.render())
        self.assertEqual(home.read_bytes(), (self.source / "Home.md").read_bytes())
        backups = list(destination.glob("Home.md.skmr-bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "my earlier home")
        self.assertEqual(extra.read_text(), "my extra note")
        self.copy()
        self.assertEqual(list(destination.glob("Home.md.skmr-bak-*")), backups)
        self.assertFalse(list(destination.rglob("*.tmp")))

    def test_read_agent_never_writes_even_when_the_filesystem_allows_it(self):
        report = self.copy(access="read")
        self.assertFalse(report.failed, report.render())
        self.assertFalse((self.vault / "00 System").exists())
        self.assertTrue(report.skipped)

    def test_dry_run_lists_the_copy_without_creating_directories_or_backups(self):
        report = self.copy(dry_run=True)
        self.assertFalse(report.failed, report.render())
        self.assertFalse((self.vault / "00 System").exists())
        self.assertTrue(report.skipped)

    def test_missing_bundle_and_invalid_vault_are_reported_as_failures(self):
        self.source = self.root / "missing-source"
        report = self.copy()
        self.assertTrue(report.failed)
        self.assertFalse((self.vault / "00 System").exists())
        self.source = self.root / "package" / "00 System"
        bare = self.root / "not-an-obsidian-vault"
        bare.mkdir()
        report = self.copy(vault=bare)
        self.assertTrue(report.failed)
        self.assertFalse((bare / "00 System").exists())

    def test_unknown_grant_cannot_seed_a_vault(self):
        report = self.copy(access="")
        self.assertTrue(report.failed)
        self.assertFalse((self.vault / "00 System").exists())

    def test_manual_installation_uses_the_selected_vault_and_local_grant(self):
        for access in ("write", "read"):
            with self.subTest(access=access):
                vault = self.root / f"manual-{access}"
                (vault / ".obsidian").mkdir(parents=True)
                arguments = ["install.py", "--method", "B", "--no-packages",
                    "--claude-dir", str(self.root / f"claude-{access}"),
                    "--agentcomm-dir", str(self.root / f"agentcomm-{access}")]
                answers = {"SELF_ACCESS": access, "SELF_HOST_ROLE": "consumer", "PEER": "peer"}
                with contextlib.ExitStack() as stack:
                    stack.enter_context(patch.object(sys, "argv", arguments))
                    stack.enter_context(patch.object(installer.platform, "system", return_value="Linux"))
                    stack.enter_context(patch.object(installer, "VAULT_SYSTEM_SRC", self.source))
                    stack.enter_context(patch.object(installer, "prompt_vault", return_value=vault))
                    stack.enter_context(patch.object(installer, "prompt_identity", return_value=answers))
                    stack.enter_context(patch("builtins.input", side_effect=AssertionError("unexpected prompt")))
                    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                    for name in ("install_knowledge", "prompt_hackerone", "configure_samba",
                                 "materialise_skills", "seed_review_queue", "verify"):
                        stack.enter_context(patch.object(installer, name))
                    self.assertEqual(installer.main(), 0)
                target = vault / "00 System" / "SKMR" / "SKMR.md"
                if access == "write":
                    self.assertEqual(target.read_bytes(), (self.source / "SKMR" / "SKMR.md").read_bytes())
                else:
                    self.assertFalse((vault / "00 System").exists())

    @unittest.skipUnless(sys.platform.startswith("linux"), "symlink checks require Linux")
    def test_symlink_destinations_are_rejected_before_any_file_is_copied(self):
        outside = self.root / "outside"
        outside.mkdir()
        destination = self.vault / "00 System"
        for nested in (False, True):
            with self.subTest(nested=nested):
                if nested:
                    destination.mkdir()
                    (destination / "SKMR").symlink_to(outside, target_is_directory=True)
                else:
                    destination.symlink_to(outside, target_is_directory=True)
                try:
                    report = self.copy()
                    self.assertTrue(report.failed)
                    self.assertEqual(list(outside.iterdir()), [])
                    self.assertFalse((destination / "Home.md").exists())
                finally:
                    if nested:
                        (destination / "SKMR").unlink()
                        destination.rmdir()
                    else:
                        destination.unlink()

    def test_directory_file_conflict_is_reported_before_any_file_is_copied(self):
        destination = self.vault / "00 System"
        destination.mkdir()
        (destination / "SKMR").write_text("keep this file", encoding="utf-8")
        report = self.copy()
        self.assertTrue(report.failed)
        self.assertEqual((destination / "SKMR").read_text(), "keep this file")
        self.assertFalse((destination / "Home.md").exists())


if __name__ == "__main__":
    unittest.main()
