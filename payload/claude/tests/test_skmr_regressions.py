"""Behavioral regressions; all write tests use temporary vaults and state."""
import contextlib
import copy
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

BASE = Path('/root/.claude')
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    obj = importlib.util.module_from_spec(spec); spec.loader.exec_module(obj)
    return obj

W = load('reg_writer', BASE/'skills/skmr/scripts/obsidian_memory.py')
G = load('reg_guard', BASE/'bin/security-consultation-guard.py')
S = load('reg_start', BASE/'bin/security-kb-session-start.py')
Q = load('reg_queue', BASE/'bin/security-report-queue.py')

def run_hook(path, event, state):
    return subprocess.run(
        [sys.executable, str(path)], input=json.dumps(event), text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, 'SKMR_STATE_DIR': str(state)}, check=False,
    )

class Regressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.vault = root/'vault'; self.vault.mkdir()
        self.store = W.VaultStore(self.vault, root/'state')
        for name, other in [('Anchor','Index'),('Index','Anchor')]:
            (self.vault/(name+'.md')).write_text(f'---\ntitle: {name}\ntype: reference\narea: reference\nstatus: user-provided\nupdated: 2026-09-10\n---\n# {name}\n\nNavigation for {name}.\n\n## Related\n- [[{other}]]\n')
    def tearDown(self):
        self.tmp.cleanup()
    def item(self, **changes):
        result = dict(title='Audit', semantic_class='methodology', content='Check a == b.', links=['Anchor'], explicit_user_request=True)
        result.update(changes); return result
    def commit(self, item):
        p = self.store.preview(item)
        return self.store.commit(p['token'])
    def test_operator_and_metadata_updates(self):
        self.commit(self.item())
        for changes in [dict(content='Check a != b.'),dict(links=['Index']),dict(tags=['new']),dict(evidence=[dict(kind='test',locator='new evidence')])]:
            candidate = self.item(**changes)
            self.assertEqual(self.store.preview(candidate)['operation'],'update')
            self.commit(candidate)
        self.assertEqual(self.store.preview(candidate)['operation'],'unchanged')
    def test_missing_links_denied_before_and_after_preview(self):
        with self.assertRaises(W.PolicyDenied): self.store.preview(self.item(links=['Missing']))
        p = self.store.preview(self.item())
        (self.vault/'Anchor.md').unlink()
        with self.assertRaises(W.PolicyDenied): self.store.commit(p['token'])
        self.assertFalse(list(self.vault.rglob('Audit.md')))
    def test_every_metadata_field_dlp(self):
        cases = [dict(status='password: MOCK_ONLY_12345'),dict(title='password: MOCK_ONLY_12345'),dict(tags=['password: MOCK_ONLY_12345']),dict(links=['password: MOCK_ONLY_12345']),dict(evidence=[dict(kind='password: MOCK_ONLY_12345',locator='fixture')])]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(W.PolicyDenied): self.store.preview(self.item(**changes))
        self.assertFalse(list(self.vault.rglob('Audit.md')))
    def test_bare_vendor_credentials_are_denied(self):
        # Self-identifying credential formats leak with no "key:"/"password:"
        # in front of them, so the content gate must recognise them on sight.
        samples = ['ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789',
                   'github_pat_11ABCDEFG0aBcDeFgHiJkL_MnOpQrStUvWxYz0123456789AbCdEfGh',
                   'AKIAIOSFODNN7EXAMPLE', 'ASIAIOSFODNN7EXAMPLE',
                   # Assemble this synthetic fixture so published test sources
                   # do not contain a complete Slack token-shaped literal.
                   '-'.join(('xoxb', '123456789012', '1234567890123', 'AbCdEfGhIjKlMnOpQrStUvWx')),
                   'sk-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789AbCdEf',
                   'sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789',
                   'AIzaSyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q']
        for secret in samples:
            with self.subTest(secret=secret[:12]):
                self.assertTrue(W.find_sensitive(secret), f'not detected: {secret[:12]}')
                with self.assertRaises(W.PolicyDenied):
                    self.store.preview(self.item(content=f'Observed value {secret} in the response. [[Anchor]]'))
        self.assertFalse(list(self.vault.rglob('Audit.md')))
    def test_credential_patterns_do_not_flag_ordinary_prose(self):
        benign = ['Token auth: raw token in `Authorization` (no `Bearer`), plus a dated version header.',
                  'Secrets come back masked as abc***xyz for readers as well as owners.',
                  'The sk-learn pipeline was not part of this assessment.']
        for text in benign:
            with self.subTest(text=text[:24]):
                self.assertEqual(W.find_sensitive(text), [])
    def test_duplicate_content_other_title(self):
        self.commit(self.item())
        with self.assertRaises(W.PolicyDenied): self.store.preview(self.item(title='Audit Copy'))
    def test_reclassification_fails_without_mutation(self):
        target = Path(self.commit(self.item())['target']); before=target.read_bytes()
        with self.assertRaisesRegex(W.PolicyDenied,'PARA destination conflicts'):
            self.store.preview(self.item(semantic_class='completed-project'))
        self.assertEqual(target.read_bytes(),before)
        moved = self.commit(self.item(semantic_class='completed-project',move_existing=True))
        self.assertEqual(moved['operation'],'move')
        self.assertIn('/04 Archives/',moved['target'])
        self.assertFalse(target.exists())
        self.assertTrue(self.store.validate_vault()['ok'])
    def test_guard_actual_events(self):
        commands=['touch audit.md', 'python3 -c "open(\'audit.md\',\'w\').write(\'fixture\')"', '/usr/bin/python3 '+G.WRITER+' validate > audit.md', '/usr/bin/python3 '+G.WRITER+' validate | tee audit.md']
        for command in commands:
            event=dict(hook_event_name='PreToolUse', tool_name='Bash',cwd=str(G.VAULT),tool_input={'command':command})
            self.assertEqual(G.handle(event)['hookSpecificOutput']['permissionDecision'],'deny')
        event['tool_input']['command']='/usr/bin/python3 '+G.WRITER+' validate'
        self.assertIsNone(G.handle(event))
        event['tool_input']['command']='cat audit.md'
        self.assertIsNone(G.handle(event))
    def test_queue_uses_same_lock(self):
        self.assertEqual(S.LOCK_FILE,Q.LOCK)
        # A held startup lock must also block the queue CLI.
        import fcntl, subprocess, sys, os
        state = Path(self.tmp.name)/'queue'; state.mkdir()
        lock = state / Q.LOCK.name
        with lock.open('w') as handle:
            fcntl.flock(handle,fcntl.LOCK_EX)
            p=subprocess.Popen([sys.executable,str(BASE/'bin/security-report-queue.py'),'status'],env={**os.environ,'SKMR_STATE_DIR':str(state)},stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                with self.assertRaises(subprocess.TimeoutExpired): p.communicate(timeout=.2)
                fcntl.flock(handle,fcntl.LOCK_UN)
                stdout,stderr=p.communicate(timeout=5)
                self.assertEqual(p.returncode,0,stderr); self.assertEqual(stdout.strip(),b'0')
            finally:
                if p.poll() is None: p.kill(); p.wait()
    def test_cached_startup_does_not_fetch(self):
        root=Path(self.tmp.name)/'startup'; root.mkdir()
        cache=root/'cache.json'
        cache.write_text(json.dumps(dict(ok=True,updated_at=datetime.now(timezone.utc).isoformat())))
        with patch.object(S,'STATE_DIR',root),patch.object(S,'LOCK_FILE',root/'queue.lock'),patch.object(S,'LAST_UPDATE',cache),patch.object(S,'VAULT',self.vault),patch.object(S,'load_pending_payload',return_value={'reports':[]}),patch.object(S,'update_repo',side_effect=AssertionError('unexpected fetch')),patch.object(S,'emit_vnext') as emit,patch('sys.stdin',io.StringIO('{}')):
            S.main(); emit.assert_called_once()

    def test_direct_security_command_activates_state_without_context(self):
        state = Path(self.tmp.name)/'command-state'
        hook = BASE/'bin/security-workflow-reminder.py'
        session = 'slash-command'
        result = run_hook(hook, {'session_id': session, 'prompt': '/claude-bughunter:autopilot target.example'}, state)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '')
        path = state/'skmr-active-sessions'/(G.digest(session)+'.json')
        self.assertTrue(json.loads(path.read_text())['active'])
        ordinary = run_hook(hook, {'session_id': 'ordinary-command', 'prompt': '/format notes.md'}, state)
        self.assertEqual(ordinary.stdout, '')
        path = state/'skmr-active-sessions'/(G.digest('ordinary-command')+'.json')
        self.assertFalse(json.loads(path.read_text())['active'])

    def test_security_skill_activates_parent_state_for_subagent(self):
        state = Path(self.tmp.name)/'skill-state'
        workflow = BASE/'bin/security-workflow-reminder.py'
        guard = BASE/'bin/security-consultation-guard.py'
        context = BASE/'bin/security-context-reminder.py'
        session = 'skill-propagation'
        run_hook(workflow, {'session_id': session, 'prompt': 'Continue the task.'}, state)
        marked = run_hook(guard, {
            'session_id': session, 'hook_event_name': 'PreToolUse', 'tool_name': 'Skill',
            'tool_input': {'skill': 'BugBountyWorkflow'},
        }, state)
        self.assertEqual(marked.stdout, '')
        propagated = run_hook(context, {'session_id': session, 'hook_event_name': 'SubagentStart'}, state)
        self.assertIn('SKMR active in the parent turn', propagated.stdout)

    def test_recall_phrases_are_detected_without_broad_false_positive(self):
        state = Path(self.tmp.name)/'recall-state'
        hook = BASE/'bin/security-workflow-reminder.py'
        prompts = [
            'what did we do last time', 'what did we previously learn', 'do you remember',
            'recall', 'previously', 'last time', 'hatırlıyor musun', 'hatırla',
            'daha önce ne denemiştik', 'önceden ne öğrendik', 'geçen sefer ne yaptık',
            'en son ne yapmıştık',
        ]
        for index, prompt in enumerate(prompts):
            with self.subTest(prompt=prompt):
                result = run_hook(hook, {'session_id': f'recall-{index}', 'prompt': prompt}, state)
                self.assertTrue(result.stdout)
        false_positive = run_hook(hook, {
            'session_id': 'not-recall',
            'prompt': 'Previously, this function returned 42; update the local implementation.'
        }, state)
        self.assertEqual(false_positive.stdout, '')

    def test_verified_failure_limitation_can_save_but_inconclusive_cannot(self):
        limitation = self.item(
            title='Verified Limitation', automatic=True, explicit_user_request=False,
            novel=True, reusable=True, verified=True,
            evidence=[{'kind': 'authorized-test', 'locator': 'run 2026-09-10T12:00Z against staging-7 endpoint /api/v2/orders'}],
            content='A failed technique established a reusable verified limitation.',
        )
        self.assertEqual(self.store.preview(limitation)['operation'], 'create')
        inconclusive = dict(limitation, title='Inconclusive Observation', verified=False)
        with self.assertRaises(W.PolicyDenied):
            self.store.preview(inconclusive)

    def test_semantic_negative_policy_stays_targeted_and_lazy(self):
        policy = (BASE/'CLAUDE.md').read_text().casefold()
        routing = (BASE/'skills/skmr/references/routing.md').read_text().casefold()
        save = (BASE/'skills/skmr/references/save-policy.md').read_text().casefold()
        self.assertIn('concrete unresolved gap', policy)
        self.assertIn('strongly prefer one targeted consultation', policy)
        self.assertIn('never invoke every source automatically', policy)
        self.assertIn('complexity alone', routing)
        self.assertIn('never makes hackerone mandatory', routing)
        self.assertIn('negative result itself is never durable memory', save)
        self.assertIn('verified reusable limitation', save)
        self.assertIn('inconclusive', save)

class EmptyVaultBootstrap(unittest.TestCase):
    """The first note in a fresh vault must be possible; the second must not cheat.

    Measured on a newly installed machine: the vault held no notes, every
    candidate was refused with 'wikilink targets do not exist', and there was no
    way to create a target either -- so a freshly installed vault could never
    receive anything at all. The exemption is deliberately narrow.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.vault = root / 'vault'
        self.vault.mkdir()
        self.store = W.VaultStore(self.vault, root / 'state')
        self.addCleanup(self.tmp.cleanup)

    def candidate(self, title, links):
        return dict(title=title, semantic_class='reference', content=f'Body of {title}.',
                    links=links, explicit_user_request=True, status='user-provided')

    def commit(self, item):
        return self.store.commit(self.store.preview(item)['token'])

    def test_the_first_note_may_name_a_target_that_does_not_exist_yet(self):
        result = self.commit(self.candidate('First', ['Second']))
        self.assertEqual(result['operation'], 'create')
        self.assertTrue(list(self.vault.rglob('First.md')))

    def test_the_second_note_is_checked_normally(self):
        self.commit(self.candidate('First', ['Second']))
        # The vault now holds a note, so the exemption is spent.
        with self.assertRaises(W.PolicyDenied) as caught:
            self.store.preview(self.candidate('Third', ['Nowhere']))
        self.assertIn('wikilink targets do not exist', str(caught.exception))

    def test_the_promised_target_can_still_be_created_and_resolves(self):
        self.commit(self.candidate('First', ['Second']))
        self.commit(self.candidate('Second', ['First']))
        self.assertTrue(list(self.vault.rglob('Second.md')))

    def test_bootstrap_does_not_bypass_the_other_guards(self):
        # An empty vault must not become a hole in secret protection.
        with self.assertRaises(W.PolicyDenied):
            self.store.preview(self.candidate('password: MOCK_ONLY_12345', ['Second']))
        self.assertFalse(list(self.vault.rglob('*.md')))


class ReportSummaryBackfill(unittest.TestCase):
    """HackerOne publishes hacktivity_summary after disclosure, sometimes days later.

    A report queued the day it went public is stored with an empty summary, and the
    live API check only returns the newest pages -- so that id never comes back and
    nothing ever rebuilt its entry. On this host the gap currently has no victim
    (every empty queue summary is also absent upstream), which is exactly why it
    needs a test: a latent path with no visible symptom is the kind that stays
    broken. Re-queueing must also never blank a summary already on disk.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.queue = Path(self.tmp.name) / 'security-new-reports.json'
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(patch.object(S, 'PENDING_REPORTS', self.queue))
        self.addCleanup(self.stack.close)
        self.addCleanup(self.tmp.cleanup)

    def _write_queue(self, reports):
        self.queue.write_text(json.dumps({
            'detected_at': datetime.now(timezone.utc).isoformat(),
            'reports': reports,
        }), encoding='utf-8')

    @staticmethod
    def _dataset_report(identifier, summary):
        return {
            'id': identifier,
            'attributes': {'title': 't', 'severity_rating': 'Medium', 'cwe': 'c',
                           'url': f'https://hackerone.com/reports/{identifier}',
                           'disclosed_at': '2026-09-12T19:08:55.117Z'},
            'relationships': {
                'program': {'data': {'attributes': {'name': 'p'}}},
                'report_generated_content': {'data': {'attributes': {'hacktivity_summary': summary}}},
            },
        }

    def test_a_late_published_summary_reaches_an_already_queued_report(self):
        self._write_queue([{'id': '111', 'title': 't', 'summary': ''}])
        filled = S.backfill_queued_summaries([self._dataset_report('111', 'published later')])
        self.assertEqual(filled, 1)
        stored = json.loads(self.queue.read_text())['reports']
        self.assertEqual(stored[0]['summary'], 'published later')

    def test_an_existing_summary_is_never_overwritten(self):
        self._write_queue([{'id': '111', 'title': 't', 'summary': 'operator already read this'}])
        self.assertEqual(S.backfill_queued_summaries([self._dataset_report('111', 'different text')]), 0)
        stored = json.loads(self.queue.read_text())['reports']
        self.assertEqual(stored[0]['summary'], 'operator already read this')

    def test_a_report_with_no_upstream_summary_stays_empty_without_a_rewrite(self):
        self._write_queue([{'id': '111', 'title': 't', 'summary': ''}])
        before = self.queue.read_bytes()
        self.assertEqual(S.backfill_queued_summaries([self._dataset_report('111', '')]), 0)
        self.assertEqual(self.queue.read_bytes(), before, 'no change must not rewrite the queue')

    def test_backfill_is_idempotent(self):
        self._write_queue([{'id': '111', 'title': 't', 'summary': ''}])
        dataset = [self._dataset_report('111', 'published later')]
        self.assertEqual(S.backfill_queued_summaries(dataset), 1)
        self.assertEqual(S.backfill_queued_summaries(dataset), 0)

    def test_requeueing_without_a_summary_does_not_blank_the_stored_one(self):
        self._write_queue([{'id': '111', 'title': 't', 'summary': 'already known'}])
        S.queue_new_reports([self._dataset_report('111', '')])
        stored = json.loads(self.queue.read_text())['reports']
        self.assertEqual(stored[0]['summary'], 'already known')

    def test_requeueing_with_a_summary_still_updates_it(self):
        self._write_queue([{'id': '111', 'title': 't', 'summary': ''}])
        S.queue_new_reports([self._dataset_report('111', 'now published')])
        stored = json.loads(self.queue.read_text())['reports']
        self.assertEqual(stored[0]['summary'], 'now published')


if __name__=='__main__': unittest.main(verbosity=2)
