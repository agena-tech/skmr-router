"""Isolated permission, authenticated-client and event reminder regressions."""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import subprocess
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory()
CONF = Path(TMP.name) / 'agent.conf'
VAULT = Path(TMP.name) / 'vault'
VAULT.mkdir()
BASE = {'self': 'sondra', 'peer': 'eliza', 'vault_local': str(VAULT),
        'vault_name': 'ElizaMemory', 'token': 'fixture-token',
        'vault_permissions': {'ElizaMemory': {'sondra': 'write', 'eliza': 'read'}}}
CONF.write_text(json.dumps(BASE))
os.environ['AGENTCOMM_CONF'] = str(CONF)
os.environ['SKMR_STATE_DIR'] = str(Path(TMP.name) / 'state')
os.environ['SKMR_RUNTIME_STATE'] = str(Path(TMP.name) / 'runtime')
os.environ['SKMR_MEMORY_PATH'] = str(Path(TMP.name) / 'MEMORY.md')
sys.path.insert(0, str(ROOT / 'lib'))
sys.path.insert(0, str(ROOT))

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

# Allow the immutable baseline to import without touching /root or live config.
original_read = Path.read_text
def fixture_read(path, *args, **kwargs):
    return json.dumps(BASE) if str(path) == '/root/agentcomm/agent.conf' else original_read(path, *args, **kwargs)
with patch.object(Path, 'read_text', fixture_read):
    P = load('skmr_agent_policy', ROOT / 'lib/skmr_agent_policy.py')
    W = load('symmetry_writer', ROOT / 'skills/skmr/scripts/obsidian_memory.py')
from skmr.hooks import report_review, runtime
from skmr.commands import learn_cmd, obsidian_memory_cmd
from skmr.core import config

class Policy(unittest.TestCase):
    def setUp(self):
        CONF.write_text(json.dumps(BASE))
    def test_write_permission_follows_live_assignment_not_identity(self):
        with patch.object(Path, 'is_mount', return_value=True):
            try:
                P.check_root(P.VAULT, writable=True)
            except PermissionError as exc:
                self.fail('assigned WRITE agent must write: ' + str(exc))
            conf = dict(BASE, vault_permissions={'ElizaMemory': {'sondra': 'read', 'eliza': 'write'}})
            CONF.write_text(json.dumps(conf))
            with self.assertRaises(PermissionError):
                P.check_root(P.VAULT, writable=True)
    def test_authenticated_actor_context_restores_local_read_permission(self):
        self.assertTrue(hasattr(P, 'acting_as'), 'canonical server needs authenticated actor context')
        conf = dict(BASE, self='eliza')
        with patch.object(P, 'ROLE', 'eliza'), patch.object(Path, 'is_mount', return_value=True):
            with self.assertRaises(PermissionError):
                P.check_root(P.VAULT, True)
            with P.acting_as('sondra', conf):
                P.check_root(P.VAULT, True)
            with self.assertRaises(PermissionError):
                P.check_root(P.VAULT, True)
    def test_read_mount_does_not_require_write_os_access(self):
        with patch.object(P, 'ROLE', 'eliza'), patch.object(Path, 'is_mount', return_value=True), \
             patch.object(os, 'access', side_effect=lambda path, mode: not mode & os.W_OK):
            self.assertTrue(P.vault_mount_usable(), 'a read-only mount is still readable')
    def test_authenticated_actor_cannot_turn_empty_directory_into_mount(self):
        self.assertTrue(hasattr(P, 'acting_as'))
        with P.acting_as('sondra', BASE), patch.object(Path, 'is_mount', return_value=False):
            with self.assertRaises(PermissionError): P.check_root(P.VAULT, True)
    def test_invalid_probe_environment_uses_safe_defaults_and_compatible_cache(self):
        with patch.dict(os.environ, {'SKMR_VAULT_PROBE_TTL': 'not-a-number', 'SKMR_VAULT_PROBE_TIMEOUT': 'NaN'}):
            policy = load('symmetry_invalid_probe', ROOT / 'lib/skmr_agent_policy.py')
            self.assertEqual(policy.vault_probe_ttl(), 30)
            self.assertEqual(policy.VAULT_PROBE_TIMEOUT, 8)
            self.assertIn('value', policy._PROBE_CACHE)

class CanonicalClient(unittest.TestCase):
    def setUp(self):
        CONF.write_text(json.dumps(BASE))
        self.requests = []
        requests = self.requests
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append((self.path, self.headers.get('X-Agent-Token'), payload))
                result = {'ok': True, 'token': 'canonical-token', 'notes': 3}
                if payload.get('mode'):
                    result = dict(ok=True, query=payload['query'], mode=payload['mode'], strategy='rrf',
                        results=[dict(chunk_id='fixture', relative='Lesson.md', heading='Lesson', content='Verified reusable limitation.', score=0.8, bm25_rank=1)],
                        warnings=[], error='', bm25_count=1, vector_count=0)
                body = json.dumps(result).encode()
                self.send_response(200); self.end_headers(); self.wfile.write(body)
            def log_message(self, *args): pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.send = Path(TMP.name) / 'send.py'
        self.send.write_text('import json, urllib.request\n'
            'def _request(method, path, payload):\n'
            f' request = urllib.request.Request("http://127.0.0.1:{self.server.server_port}" + path, data=json.dumps(payload).encode(), method=method, headers={{"X-Agent-Token":"fixture-token"}})\n'
            ' with urllib.request.urlopen(request) as response: return json.load(response)\n')
        self.env = patch.dict(os.environ, {'AGENTCOMM_SEND': str(self.send)}); self.env.start()
    def tearDown(self):
        self.env.stop(); self.server.shutdown(); self.server.server_close(); self.thread.join()
    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['writer', *args]), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = W.main()
        return code, out.getvalue(), err.getvalue()
    def test_nomount_write_previews_canonical_candidate_via_authenticated_request(self):
        candidate = Path(TMP.name) / 'candidate.json'
        candidate.write_text(json.dumps({'title': 'Lesson', 'semantic_class': 'methodology'}))
        code, out, err = self.call('preview', str(candidate))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['token'], 'canonical-token')
        self.assertEqual(self.requests, [('/api/vault/action', 'fixture-token', {'action': 'preview', 'candidate': {'title': 'Lesson', 'semantic_class': 'methodology'}})])
    def test_nomount_generic_search_and_validation_use_canonical_api(self):
        for args in [('search', 'root cause'), ('validate',), ('lint',), ('search', 'name', '--profile', 'User')]:
            code, out, err = self.call(*args)
            self.assertEqual(code, 0, err)
        self.assertEqual([r[2]['action'] for r in self.requests], ['search', 'validate', 'lint', 'search'])
        self.assertEqual(self.requests[-1][2]['profile'], 'User')
    def test_read_cli_denied_even_with_actor_environment_override(self):
        CONF.write_text(json.dumps(dict(BASE, vault_permissions={'ElizaMemory': {'sondra': 'read', 'eliza': 'write'}})))
        with patch.dict(os.environ, {'SKMR_ACTOR': 'eliza'}):
            code, out, err = self.call('commit', 'canonical-token')
        self.assertNotEqual(code, 0)
        self.assertEqual(self.requests, [])
    def test_unavailable_api_reports_failure_without_save_claim(self):
        self.send.write_text('def _request(*args): raise RuntimeError("offline")\n')
        code, out, err = self.call('commit', 'token')
        self.assertNotEqual(code, 0)
        self.assertIn('offline', err)
        self.assertNotIn('saved', out.casefold())
    def test_nomount_generic_command_preserves_remote_search_mode_and_results(self):
        conf = config.load()
        previous = conf['vault_path']; conf['vault_path'] = str(VAULT)
        capture = io.StringIO()
        try:
            with contextlib.redirect_stdout(capture):
                code = obsidian_memory_cmd.run(['search', 'root cause', '--mode', 'bm25', '--limit', '2'], None)
        finally: conf['vault_path'] = previous
        self.assertEqual(code, 0)
        self.assertIn('Lesson.md', capture.getvalue())
        self.assertEqual(self.requests[-1][2], {'action': 'search', 'query': 'root cause', 'mode': 'bm25', 'limit': 2})

class Reminders(unittest.TestCase):
    def setUp(self): CONF.write_text(json.dumps(BASE))
    def test_report_protocol_uses_assigned_write_permission(self):
        protocol = report_review.protocol()
        self.assertIn('commit <preview-token>', protocol)
        self.assertNotIn('Sondra has READ-ONLY', protocol)
    def test_read_event_reminder_is_small_and_does_not_consume_inbox(self):
        CONF.write_text(json.dumps(dict(BASE, vault_permissions={'ElizaMemory': {'sondra': 'read', 'eliza': 'write'}})))
        result = runtime.run_event({'hook_event_name': 'PostToolUse', 'tool_name': 'Skill', 'tool_input': {'skill': 'skmr', 'args': 'save a verified reusable lesson'}})
        self.assertIsNotNone(result, 'qualifying READ event needs delegation reminder')
        text = result['hookSpecificOutput']['additionalContext']
        self.assertIn('candidate', text.casefold())
        self.assertIn('eliza', text.casefold())
        self.assertLess(len(text), 1200)
    def test_write_events_and_unrelated_read_events_do_not_remind(self):
        self.assertIsNone(runtime.run_event({'hook_event_name': 'PostToolUse', 'tool_name': 'Read', 'tool_input': {'file_path': '/tmp/ordinary.txt'}}))
        self.assertIsNone(runtime.run_event({'hook_event_name': 'PostToolUse', 'tool_name': 'Skill', 'tool_input': {'skill': 'skmr'}}))

class CommandDelegation(unittest.TestCase):
    def setUp(self):
        CONF.write_text(json.dumps(dict(BASE, vault_permissions={'ElizaMemory': {'sondra': 'read', 'eliza': 'write'}})))
    def test_read_qualifying_learning_episode_sends_staged_candidate(self):
        delivered = Path(TMP.name) / 'delivered.json'
        send = Path(TMP.name) / 'learning-send.py'
        send.write_text('import json,sys\nfrom pathlib import Path\n'
            'assert sys.argv[1] == "candidate"\n'
            f'Path({str(delivered)!r}).write_text(Path(sys.argv[2]).read_text())\n')
        args = ['--topic', 'Reusable limitation', '--problem', 'An input escaped validation',
                '--solution', 'Validate before routing', '--lesson', 'Always validate path components before writing because aliases can escape the intended storage boundary.',
                '--verified', '--evidence', 'code=fixture.py:1', '--link', 'Canonical policy']
        capture = io.StringIO()
        with patch.dict(os.environ, {'AGENTCOMM_SEND': str(send)}), contextlib.redirect_stdout(capture):
            code = learn_cmd.run(args, None)
        self.assertEqual(code, 0)
        self.assertTrue(delivered.exists(), 'qualifying READ episode must deliver candidate automatically')
        self.assertTrue(json.loads(delivered.read_text())['verified'])
        self.assertIn('not saving', capture.getvalue().casefold())

class GuardAliases(unittest.TestCase):
    def test_configured_windows_and_mount_aliases_are_guarded_for_direct_writes(self):
        CONF.write_text(json.dumps(dict(BASE, vault_windows_path=r'C:\Memory\Shared')))
        guard = load('symmetry_guard', ROOT / 'bin/security-consultation-guard.py')
        for path in [str(VAULT / 'note.md'), '/mnt/c/Memory/Shared/note.md', r'C:\Memory\Shared\note.md']:
            result = guard.handle({'hook_event_name': 'PreToolUse', 'tool_name': 'Write', 'tool_input': {'file_path': path, 'content': 'direct'}})
            self.assertIsNotNone(result, 'guard must protect configured alias: ' + path)
            self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')

class StartupIntegrity(unittest.TestCase):
    def test_nomount_startup_verifies_canonical_api_and_reports_assigned_mode(self):
        CONF.write_text(json.dumps(BASE))
        startup = load('symmetry_startup', ROOT / 'bin/security-kb-session-start.py')
        writer = Path(TMP.name) / 'startup-writer.py'
        writer.write_text('import json\nprint(json.dumps({"ok":True,"notes":7}))\n')
        with patch.dict(os.environ, {'SKMR_WRITER': str(writer)}), \
             patch.object(startup, 'vault_available', return_value=True), \
             patch.object(startup, 'vault_mount_usable', return_value=False):
            ok, text = startup.vault_integrity_status()
        self.assertTrue(ok)
        self.assertIn('validate ok (7 notes)', text)
        self.assertIn('WRITE', text)
        self.assertNotIn('no writes', text)

if __name__ == '__main__': unittest.main()
