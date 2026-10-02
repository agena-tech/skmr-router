"""Registry consistency between slash-command and Skill security classification."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path('/root/.claude')
WORKFLOW = BASE/'bin/security-workflow-reminder.py'
GUARD = BASE/'bin/security-consultation-guard.py'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    obj = importlib.util.module_from_spec(spec); spec.loader.exec_module(obj)
    return obj


sys.path.insert(0, str(BASE/'lib'))
# Imported by module name so the hooks' own import resolves to this exact
# object; the identity assertions below are what actually prove one source.
import skmr_security_registry as R  # noqa: E402
W = load('reg_workflow', WORKFLOW)
G = load('reg_guard_registry', GUARD)


def run_hook(path, event, state):
    return subprocess.run(
        [sys.executable, str(path)], input=json.dumps(event), text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, 'SKMR_STATE_DIR': str(state)}, check=False,
    )


class SecurityRegistry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)/'state'

    def tearDown(self):
        self.tmp.cleanup()

    def active(self, session):
        path = self.state/'skmr-active-sessions'/(G.digest(session)+'.json')
        if not path.exists():
            return None
        return json.loads(path.read_text())['active']

    def pre_skill(self, session, skill, **extra):
        return run_hook(GUARD, {
            'session_id': session, 'hook_event_name': 'PreToolUse', 'tool_name': 'Skill',
            'tool_input': {'skill': skill, **extra},
        }, self.state)

    def test_direct_security_command_activates_with_zero_context_bytes(self):
        for prompt in ('/autopilot', '/claude-bughunter:autopilot target.example',
                       '/recon', '/bugbountyskills', '/skmr'):
            with self.subTest(prompt=prompt):
                session = 'cmd-'+prompt
                result = run_hook(WORKFLOW, {'session_id': session, 'prompt': prompt}, self.state)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(result.stdout.encode('utf-8')), 0)
                self.assertTrue(self.active(session))

    def test_security_skill_tool_activates_state(self):
        for skill in ('autopilot', 'claude-bughunter:autopilot', 'recon', 'AUTOPILOT',
                      'bugbountyskills', 'hackerone-intelligence', 'web3-audit', 'skmr'):
            with self.subTest(skill=skill):
                session = 'skill-'+skill
                result = self.pre_skill(session, skill)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(self.active(session))

    def test_non_security_skill_does_not_activate(self):
        for skill in ('ecc:python-patterns', 'ecc:git-workflow', 'artifact-design'):
            with self.subTest(skill=skill):
                session = 'neutral-'+skill
                result = self.pre_skill(session, skill)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIsNone(self.active(session))

    def test_consultation_deduplication_survives_registry_change(self):
        for skill in ('bugbountyskills', 'hackerone-intelligence'):
            with self.subTest(skill=skill):
                session = 'dedup-'+skill
                args = {'args': 'jwt kid path traversal'}
                first = self.pre_skill(session, skill, **args)
                self.assertEqual(first.stdout, '')
                self.assertTrue(self.active(session))
                done = run_hook(GUARD, {
                    'session_id': session, 'hook_event_name': 'PostToolUse', 'tool_name': 'Skill',
                    'tool_input': {'skill': skill, **args}, 'tool_response': {'success': True},
                }, self.state)
                self.assertEqual(done.stdout, '')
                repeat = self.pre_skill(session, skill, **args)
                decision = json.loads(repeat.stdout)['hookSpecificOutput']
                self.assertEqual(decision['permissionDecision'], 'deny')
                self.assertIn('already succeeded', decision['permissionDecisionReason'])
                other = self.pre_skill(session, skill, args='unrelated second question')
                self.assertEqual(other.stdout, '')

    def test_both_hooks_share_one_registry_object(self):
        self.assertIs(W.is_security_command, R.is_security_command)
        self.assertIs(G.is_security_skill, R.is_security_skill)
        for module, name in ((W, 'SECURITY_SKILLS'), (W, 'SECURITY_COMMANDS'),
                             (G, 'SECURITY_SKILLS'), (G, 'SECURITY_COMMANDS')):
            self.assertFalse(hasattr(module, name), f'{name} must not be redefined locally')
        for hook in (WORKFLOW, GUARD):
            source = hook.read_text(encoding='utf-8')
            self.assertNotIn('SECURITY_SKILL_NAMES = {', source)
            self.assertNotIn('SECURITY_COMMANDS = {', source)
            self.assertNotIn('SECURITY_SKILLS = {', source)

    def test_registry_is_normalized_and_derived(self):
        for group in (R.SECURITY_SKILL_NAMES, R.SECURITY_COMMAND_ONLY_NAMES, R.SECURITY_SKILL_ONLY_NAMES):
            for name in group:
                self.assertEqual(name, R.normalize(name))
        self.assertEqual(R.SECURITY_SKILL_NAMES & R.SECURITY_COMMAND_ONLY_NAMES, frozenset())
        self.assertLessEqual(R.SECURITY_SKILL_ONLY_NAMES, R.SECURITY_SKILL_NAMES)
        self.assertEqual(
            R.SECURITY_COMMANDS,
            (R.SECURITY_SKILL_NAMES - R.SECURITY_SKILL_ONLY_NAMES) | R.SECURITY_COMMAND_ONLY_NAMES,
        )

    def test_no_slash_skill_classification_drift(self):
        """Every Skill-invocable security name is also a security slash command,
        and every slash command is a Skill name unless declared command-only."""
        drift = (R.SECURITY_SKILL_NAMES - R.SECURITY_SKILL_ONLY_NAMES) - R.SECURITY_COMMANDS
        self.assertEqual(drift, frozenset(), f'skills unreachable as commands: {sorted(drift)}')
        undeclared = R.SECURITY_COMMANDS - R.SECURITY_SKILL_NAMES - R.SECURITY_COMMAND_ONLY_NAMES
        self.assertEqual(undeclared, frozenset(), f'commands with undeclared skill status: {sorted(undeclared)}')
        for name in ('autopilot', 'recon', 'bugbountyskills', 'hackerone-intelligence', 'skmr'):
            with self.subTest(name=name):
                self.assertTrue(R.is_security_skill(name))
                self.assertTrue(R.is_security_command(name))

    def test_installed_names_are_classified_consistently(self):
        """Guards against a registry entry silently losing its installed backing."""
        installed_skills = {p.parent.name.casefold()
                            for p in (BASE/'skills').glob('*/SKILL.md')}
        installed_commands = set()
        # A machine with no Claude Code plugins has no marketplace directory at
        # all. Iterating it unconditionally turned that correct state into a
        # FileNotFoundError, so a fresh installation could never pass.
        marketplaces = BASE/'plugins/marketplaces'
        if marketplaces.is_dir():
            for marketplace in marketplaces.iterdir():
                installed_skills |= {p.parent.name.casefold()
                                     for p in (marketplace/'skills').glob('*/SKILL.md')}
                installed_commands |= {p.stem.casefold()
                                       for p in (marketplace/'commands').glob('*.md')}
        for name in R.SECURITY_COMMAND_ONLY_NAMES:
            with self.subTest(name=name):
                self.assertNotIn(name, installed_skills,
                                 'installed skill must live in SECURITY_SKILL_NAMES')
        if not installed_commands:
            self.skipTest('no plugin commands installed; nothing to classify')
        for name in ('autopilot', 'recon', 'web3-audit'):
            with self.subTest(name=name):
                self.assertIn(name, installed_commands)
                self.assertIn(name, R.SECURITY_SKILL_NAMES)


class VaultWriteGuard(unittest.TestCase):
    """The canonical-writer protection must not depend on path spelling."""

    def variants(self):
        canonical = str(G.VAULT)
        return {
            'canonical': canonical,
            'lowercase segments': canonical.replace('/Users/', '/users/')
                                           .replace('/OneDrive/', '/onedrive/')
                                           .replace('/Belgeler/', '/belgeler/'),
            'all lowercase': canonical.lower(),
            'dot segment': canonical + '/.',
        }

    def test_case_differing_vault_paths_are_denied(self):
        # /mnt/c is case-insensitive, so these spellings name the same inode.
        for label, root in self.variants().items():
            for tool, field in (('Write', 'file_path'), ('Edit', 'file_path'),
                                ('MultiEdit', 'file_path'), ('NotebookEdit', 'notebook_path')):
                with self.subTest(path=label, tool=tool):
                    result = G.handle({
                        'hook_event_name': 'PreToolUse', 'tool_name': tool, 'session_id': 's',
                        'tool_input': {field: root + '/Notes/probe.md'},
                    })
                    self.assertIsNotNone(result, f'{tool} via {label} was allowed')
                    self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_case_differing_vault_shell_mutation_is_denied(self):
        for label, root in self.variants().items():
            with self.subTest(path=label):
                result = G.handle({
                    'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'session_id': 's',
                    'cwd': '/root/.claude',
                    'tool_input': {'command': f'printf x > {root}/Notes/probe.md'},
                })
                self.assertIsNotNone(result, f'shell write via {label} was allowed')

    def test_paths_outside_the_vault_stay_allowed(self):
        for path in ('/root/.claude/notes/x.md', '/tmp/x.md',
                     str(G.VAULT) + '-sibling/x.md', str(G.VAULT).lower() + '-sibling/x.md'):
            with self.subTest(path=path):
                self.assertIsNone(G.handle({
                    'hook_event_name': 'PreToolUse', 'tool_name': 'Write', 'session_id': 's',
                    'tool_input': {'file_path': path},
                }))

    def test_guard_covers_every_write_capable_tool_in_the_matcher(self):
        settings = json.loads((BASE/'settings.json').read_text(encoding='utf-8'))
        matchers = [entry.get('matcher', '') for entry in settings['hooks']['PreToolUse']]
        matcher = next(m for m in matchers if 'Write' in m)
        for tool in ('Write', 'Edit', 'MultiEdit', 'NotebookEdit'):
            with self.subTest(tool=tool):
                self.assertIn(tool, matcher.split('|'))


class SecurityStateLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)/'state'

    def tearDown(self):
        self.tmp.cleanup()

    def prompt(self, session, text):
        return run_hook(WORKFLOW, {'session_id': session, 'prompt': text}, self.state)

    def read(self, session):
        path = self.state/'skmr-active-sessions'/(G.digest(session)+'.json')
        return json.loads(path.read_text()) if path.exists() else None

    def test_engagement_stays_armed_across_neutral_follow_ups(self):
        session = 'engagement'
        self.assertEqual(self.prompt(session, '/autopilot acme.example').stdout, '')
        self.assertTrue(self.read(session)['active'])
        for follow_up in ('devam et', 'simdi sonraki endpointe bak', 'tamam'):
            with self.subTest(follow_up=follow_up):
                result = self.prompt(session, follow_up)
                # Sticky state must never re-emit the reminder.
                self.assertEqual(len(result.stdout.encode('utf-8')), 0)
                state = self.read(session)
                self.assertTrue(state['active'], 'engagement disarmed by a neutral prompt')
                self.assertFalse(state['turn_active'])
        for hook, event in ((BASE/'bin/security-context-reminder.py', 'SubagentStart'),
                            (BASE/'bin/security-failure-reminder.py', 'PostToolUseFailure')):
            with self.subTest(hook=hook.name):
                out = run_hook(hook, {'session_id': session, 'hook_event_name': event}, self.state)
                self.assertTrue(out.stdout, f'{event} lost SKMR context mid-engagement')

    def test_unrelated_session_never_becomes_active(self):
        session = 'plain'
        for text in ('bu dosyayi yeniden adlandir', 'tesekkurler', 'testleri calistir'):
            self.assertEqual(self.prompt(session, text).stdout, '')
        self.assertFalse(self.read(session)['active'])

    def test_stickiness_does_not_leak_between_sessions(self):
        self.prompt('sec-one', '/autopilot acme.example')
        self.prompt('sec-two', 'rename this file')
        self.assertTrue(self.read('sec-one')['active'])
        self.assertFalse(self.read('sec-two')['active'])

    def test_consultation_dedup_is_scoped_to_one_turn_without_state(self):
        session = 'no-state'
        command = '/usr/bin/python3 /root/.claude/knowledge/bugskill-ai/search_reports.py --query idor'
        def bash(event, response=None):
            payload = {'session_id': session, 'hook_event_name': event, 'tool_name': 'Bash',
                       'tool_input': {'command': command}}
            if response:
                payload['tool_response'] = response
            return run_hook(GUARD, payload, self.state)
        self.assertEqual(bash('PreToolUse').stdout, '')
        self.assertEqual(bash('PostToolUse', {'success': True}).stdout, '')
        self.assertIn('already succeeded', bash('PreToolUse').stdout)
        self.assertNotEqual(self.read(session)['turn_id'], 'unknown')
        # A new user turn must reopen the bucket rather than deny forever.
        self.prompt(session, 'peki bir de bunu ara')
        self.assertEqual(bash('PreToolUse').stdout, '')


class SecuritySkillCoverage(unittest.TestCase):
    """Every installed offensive-security skill must reach SKMR state."""

    def bughunter(self, kind, pattern):
        root = BASE/'plugins/marketplaces/elementalsouls'/kind
        if not root.is_dir():
            return set()          # the plugin is not installed on this machine
        return {(p.parent.name if kind == 'skills' else p.stem).casefold()
                for p in root.glob(pattern)}

    def test_every_bughunter_skill_and_command_is_registered(self):
        installed = self.bughunter('skills', '*/SKILL.md') | self.bughunter('commands', '*.md')
        if not installed:
            # The rule is that every INSTALLED offensive-security skill reaches
            # SKMR state. A machine without the bughunter plugin has none, which
            # is a correct state; asserting otherwise made the suite require an
            # optional third-party plugin to be present.
            self.skipTest('bughunter plugin not installed; nothing to register')
        missing = sorted(installed - R.SECURITY_SKILL_NAMES)
        self.assertEqual(missing, [], f'unregistered claude-bughunter entries: {missing}')

    def test_representative_attack_skills_activate(self):
        for name in ('hunt-idor', 'hunt-ssrf', 'hunt-rce', 'bug-bounty', 'offensive-osint',
                     'm365-entra-attack', 'okta-attack', 'apk-redteam-pipeline',
                     'security-arsenal', 'web2-recon', 'triage-validation'):
            with self.subTest(name=name):
                self.assertTrue(R.is_security_skill(f'claude-bughunter:{name}'))

    def test_ecc_vulnerability_skills_activate(self):
        for name in ('security-review', 'security-scan', 'security-bounty-hunter',
                     'defi-amm-security', 'llm-trading-agent-security', 'django-security',
                     'laravel-security', 'perl-security', 'quarkus-security',
                     'springboot-security'):
            with self.subTest(name=name):
                self.assertTrue(R.is_security_skill(f'ecc:{name}'))

    def test_skmr_installed_skills_are_all_classified(self):
        """security-kb-session-start.py installs skills at runtime; none may
        end up unclassified just because this registry was not edited."""
        inventory = json.loads(R.MANAGED_SKILLS_FILE.read_text(encoding='utf-8'))
        installed = [name for name in inventory['skills'] if name.strip()]
        self.assertTrue(installed, 'managed-skill inventory unexpectedly empty')
        for name in installed:
            with self.subTest(name=name):
                self.assertTrue(R.is_security_skill(name))
        local = {p.parent.name for p in (BASE/'skills').glob('*/SKILL.md')}
        self.assertFalse(R.is_security_skill('send'), 'messaging alone must not activate security retrieval')
        for name in sorted(local - {'send', 'ccg'}):
            with self.subTest(name=name):
                self.assertTrue(R.is_security_skill(name), f'local skill {name} unclassified')

    def test_newly_synced_skill_is_security_without_editing_the_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)/'state'
            state.mkdir()
            fresh = 'brand-new-bugskill-technique'
            self.assertNotIn(fresh, R.SECURITY_SKILL_NAMES)
            (state/'security-managed-skills.json').write_text(json.dumps({
                'repository': 'https://example.invalid/bugskill.git',
                'skills': ['403Bypass', fresh],
                'updated_at': '2026-09-10T00:00:00+00:00',
            }), encoding='utf-8')
            session = 'synced'
            result = run_hook(GUARD, {
                'session_id': session, 'hook_event_name': 'PreToolUse', 'tool_name': 'Skill',
                'tool_input': {'skill': fresh},
            }, state)
            self.assertEqual(result.returncode, 0, result.stderr)
            path = state/'skmr-active-sessions'/(G.digest(session)+'.json')
            self.assertTrue(path.exists() and json.loads(path.read_text())['active'])

    def test_damaged_inventory_falls_back_to_the_static_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)/'state'
            state.mkdir()
            for broken in ('not json at all', '{"skills": "oops"}', '[]'):
                (state/'security-managed-skills.json').write_text(broken, encoding='utf-8')
                session = 'broken-'+broken[:6]
                result = run_hook(GUARD, {
                    'session_id': session, 'hook_event_name': 'PreToolUse', 'tool_name': 'Skill',
                    'tool_input': {'skill': 'autopilot'},
                }, state)
                with self.subTest(inventory=broken):
                    self.assertEqual(result.returncode, 0, result.stderr)
                    path = state/'skmr-active-sessions'/(G.digest(session)+'.json')
                    self.assertTrue(path.exists() and json.loads(path.read_text())['active'])

    def test_ordinary_development_skills_stay_out(self):
        for name in ('ecc:vue-patterns', 'ecc:git-workflow', 'ecc:python-patterns',
                     'ecc:hipaa-compliance', 'ecc:healthcare-phi-compliance',
                     'ecc:safety-guard', 'ecc:gateguard', 'ecc:production-audit',
                     'artifact-design', 'dataviz'):
            with self.subTest(name=name):
                self.assertFalse(R.is_security_skill(name))


WRITER = load('reg_writer_quality', BASE/'skills/skmr/scripts/obsidian_memory.py')


class VaultFixture(unittest.TestCase):
    """A small vault with realistic frontmatter, links, tags and ages."""

    NOTES = {
        'JWT Kid Path Traversal': dict(
            type='methodology', area='reference', tags=['jwt', 'auth-bypass'], age=10,
            body='Signing key selection follows the kid header.\nA traversal payload in kid reaches arbitrary files.',
            links=['Token Handling Index']),
        'Token Handling Index': dict(
            type='reference', area='reference', tags=['jwt'], age=20,
            body='Navigation note for token handling work.', links=['JWT Kid Path Traversal']),
        'Cache Key Normalisation': dict(
            type='methodology', area='reference', tags=['caching'], age=400,
            body='Path confusion changes the cache key without changing the origin route.',
            links=['Token Handling Index']),
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.vault = root/'vault'
        self.vault.mkdir()
        self.store = WRITER.VaultStore(self.vault, root/'state')
        import datetime as _dt
        for title, spec in self.NOTES.items():
            updated = (_dt.date.today() - _dt.timedelta(days=spec['age'])).isoformat()
            tags = ''.join(f'\n  - {tag}' for tag in spec['tags'])
            related = ''.join(f'\n- [[{link}]]' for link in spec['links'])
            (self.vault/f'{title}.md').write_text(
                f"---\ntitle: {title}\ntype: {spec['type']}\narea: {spec['area']}\n"
                f"status: verified\nupdated: {updated}\ntags:{tags}\n---\n\n"
                f"# {title}\n\n{spec['body']}\n\n## Related{related}\n",
                encoding='utf-8')

    def tearDown(self):
        self.tmp.cleanup()


class RetrievalQuality(VaultFixture):
    def test_title_beats_tag_beats_body(self):
        result = self.store.search('traversal')
        self.assertEqual(result['matches'][0]['title'], 'JWT Kid Path Traversal')
        self.assertEqual(result['matches'][0]['score'], float(WRITER.WEIGHT_TITLE))

    def test_result_reports_which_terms_were_not_understood(self):
        result = self.store.search('jwt polyglot deserialization')
        self.assertIn('jwt', result['matched_terms'])
        self.assertEqual(sorted(result['unmatched_terms']), ['deserialization', 'polyglot'])
        self.assertLess(result['coverage'], 1.0)
        self.assertEqual(result['confidence'], 'weak')
        self.assertIn('suggestions', result['advice'])

    def test_empty_result_is_an_explicit_negative_not_silence(self):
        result = self.store.search('kubernetes admission controller')
        self.assertEqual(result['matches'], [])
        self.assertEqual(result['confidence'], 'none')
        self.assertEqual(result['matched_terms'], [])
        self.assertEqual(result['notes_scanned'], len(self.NOTES))
        self.assertIn('not proof the knowledge is absent', result['advice'])

    def test_near_miss_vocabulary_is_offered(self):
        result = self.store.search('traversl')
        self.assertIn('traversl', result['suggestions'])
        self.assertIn('traversal', result['suggestions']['traversl'])

    def test_wikilink_neighbours_are_reachable_from_a_strong_hit(self):
        result = self.store.search('kid header')
        titles = [match['title'] for match in result['matches']]
        self.assertIn('JWT Kid Path Traversal', titles)
        self.assertIn('Token Handling Index', titles)
        neighbour = next(m for m in result['matches'] if m['title'] == 'Token Handling Index')
        self.assertEqual(neighbour['via'], 'JWT Kid Path Traversal')
        self.assertLess(neighbour['score'], result['matches'][0]['score'])

    def test_results_carry_snippet_and_age(self):
        match = self.store.search('traversal')['matches'][0]
        self.assertIn('traversal', match['snippet'].casefold())
        self.assertEqual(match['age_days'], 10)
        self.assertFalse(match['stale'])
        self.assertEqual(match['status'], 'verified')

    def test_stale_notes_are_flagged_in_results_and_validation(self):
        aged = next(m for m in self.store.search('cache')['matches'] if m['title'] == 'Cache Key Normalisation')
        self.assertTrue(aged['stale'])
        self.assertGreater(aged['age_days'], WRITER.STALE_AFTER_DAYS)
        report = self.store.validate_vault()
        self.assertEqual([item['path'] for item in report['stale']], ['Cache Key Normalisation.md'])
        # Staleness is review pressure, never a validation failure on its own.
        self.assertTrue(report['ok'])

    def test_high_confidence_requires_an_identifying_hit_not_just_volume(self):
        # Only body mentions: full coverage, but nothing identifies a note.
        body_only = self.store.search('signing selection follows')
        self.assertEqual(body_only['unmatched_terms'], [])
        self.assertEqual(body_only['confidence'], 'partial')
        self.assertGreaterEqual(body_only['matches'][0]['score'], 3.0)
        # A title hit identifies it.
        identified = self.store.search('kid')
        self.assertEqual(identified['confidence'], 'high')

    def test_empty_query_still_refuses(self):
        with self.assertRaises(WRITER.PolicyDenied):
            self.store.search('   ')


class EvidenceGate(VaultFixture):
    def item(self, **changes):
        result = dict(title='Audited Technique', semantic_class='methodology',
                      content='Reusable finding worth keeping.', links=['Token Handling Index'],
                      automatic=True, explicit_user_request=False,
                      novel=True, reusable=True, verified=True)
        result.update(changes)
        return result

    def test_unfalsifiable_locator_cannot_carry_verified(self):
        for locator in ('deterministic negative fixture', 'tested', 'works as expected',
                        'confirmed', 'by inspection'):
            with self.subTest(locator=locator):
                with self.assertRaises(WRITER.PolicyDenied):
                    self.store.preview(self.item(evidence=[{'kind': 'authorized-test', 'locator': locator}]))

    def test_unknown_evidence_kind_is_refused(self):
        with self.assertRaises(WRITER.PolicyDenied):
            self.store.preview(self.item(evidence=[{'kind': 'trust-me', 'locator': '/tmp/proof.log'}]))

    def test_addressable_evidence_is_accepted(self):
        for kind, locator in (('url', 'https://example.invalid/advisory/2026-0042'),
                              ('test', 'tests/test_skmr_regressions.py::Regressions::test_operator_and_metadata_updates'),
                              ('command', 'curl -s https://staging-7.invalid/api/v2/orders?id=1041'),
                              ('measurement', '412 of 4096 codes accepted in 90 seconds'),
                              ('hackerone-report', 'HackerOne report 1731234')):
            with self.subTest(kind=kind):
                candidate = self.item(title=f'Audited {kind}', evidence=[{'kind': kind, 'locator': locator}])
                self.assertEqual(self.store.preview(candidate)['operation'], 'create')

    def test_file_evidence_must_exist(self):
        with self.assertRaises(WRITER.PolicyDenied):
            self.store.preview(self.item(evidence=[{'kind': 'file', 'locator': '/nonexistent/proof.log'}]))
        real = self.item(title='Audited File', evidence=[{'kind': 'file', 'locator': str(BASE/'settings.json')}])
        self.assertEqual(self.store.preview(real)['operation'], 'create')

    def test_narrative_evidence_may_accompany_but_not_carry(self):
        narrative = {'kind': 'advisory', 'locator': 'vendor advisory describing the class'}
        with self.assertRaises(WRITER.PolicyDenied):
            self.store.preview(self.item(evidence=[narrative]))
        accepted = self.item(title='Audited Pair', evidence=[
            narrative, {'kind': 'url', 'locator': 'https://example.invalid/advisory/2026-0042'}])
        self.assertEqual(self.store.preview(accepted)['operation'], 'create')


class StoreBoundary(unittest.TestCase):
    """Durable notes must not be smuggled into native auto-memory."""

    DURABLE = ('---\ntitle: JWT Kid Traversal\ntype: methodology\narea: reference\n'
               'status: verified\nupdated: 2026-09-10\n---\n\n# JWT Kid Traversal\n\nBody.\n')
    NATIVE = ('---\nname: current-target\ndescription: Working continuity for the active task.\n'
              'metadata:\n  type: project\n---\n\nNext step: re-run the endpoint sweep.\n')

    def native_write(self, tool, field, text):
        return G.handle({
            'hook_event_name': 'PreToolUse', 'tool_name': tool, 'session_id': 's',
            'tool_input': {'file_path': '/root/.claude/projects/-root/memory/note.md', field: text},
        })

    def test_durable_note_into_native_memory_is_denied(self):
        for tool, field in (('Write', 'content'), ('Edit', 'new_string')):
            with self.subTest(tool=tool):
                result = self.native_write(tool, field, self.DURABLE)
                self.assertIsNotNone(result)
                self.assertIn('canonical writer', result['hookSpecificOutput']['permissionDecisionReason'])

    def test_multiedit_payload_is_inspected(self):
        result = G.handle({
            'hook_event_name': 'PreToolUse', 'tool_name': 'MultiEdit', 'session_id': 's',
            'tool_input': {'file_path': '/root/.claude/projects/-root/memory/note.md',
                           'edits': [{'new_string': 'harmless'}, {'new_string': self.DURABLE}]},
        })
        self.assertIsNotNone(result)

    def test_ordinary_native_memory_writes_stay_allowed(self):
        for text in (self.NATIVE, '- [Title](file.md) — hook\n', 'plain continuity note'):
            with self.subTest(text=text[:24]):
                self.assertIsNone(self.native_write('Write', 'content', text))

    def test_durable_shape_outside_native_memory_is_not_the_guard_business(self):
        self.assertIsNone(G.handle({
            'hook_event_name': 'PreToolUse', 'tool_name': 'Write', 'session_id': 's',
            'tool_input': {'file_path': '/root/.claude/docs/example.md', 'content': self.DURABLE},
        }))


if __name__ == '__main__':
    unittest.main()
