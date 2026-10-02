#!/usr/bin/env python3
"""The read path must follow the operating rule: no LAN -> look at public.

Reading permanent memory may fall back to the token-gated HTTP vault route when
the SMB mount is gone. Writing must not: vault writes stay on the local mount,
so an HTTP-only vault is readable and never writable.
"""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

LIB = Path('/root/.claude/lib/skmr_agent_policy.py')


def load(conf_path):
    sys.argv = ['test']
    spec = importlib.util.spec_from_file_location('policy_under_test', LIB)
    module = importlib.util.module_from_spec(spec)
    import os
    previous = os.environ.get('AGENTCOMM_CONF')
    os.environ['AGENTCOMM_CONF'] = str(conf_path)
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            os.environ.pop('AGENTCOMM_CONF', None)
        else:
            os.environ['AGENTCOMM_CONF'] = previous
    return module


class VaultReadFallback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.vault = self.root / 'vault-ro'          # exists but is NOT a mount
        self.vault.mkdir()
        self.data = self.root / 'data'
        self.data.mkdir()
        self.conf = self.root / 'agent.conf'
        self.conf.write_text(json.dumps({
            'self': 'sondra', 'peer': 'eliza', 'token': 'test-token',
            'vault_local': str(self.vault),
            'api_base': 'http://127.0.0.1:1', 'api_lan': 'http://192.168.1.110:1',
        }), encoding='utf-8')
        self.p = load(self.conf)
        self.p.DATA_DIR = self.data

    def tearDown(self):
        self.tmp.cleanup()

    def public(self, url='https://tunnel.example'):
        (self.data / 'connection.json').write_text(
            json.dumps({'mode': 'public', 'public_url': url}), encoding='utf-8')

    def test_no_mount_and_no_http_stays_unavailable(self):
        self.p.vault_http_readable = lambda: False
        self.assertFalse(self.p.vault_available())

    def test_no_mount_but_http_readable_counts_as_available(self):
        self.public()
        self.p.vault_http_readable = lambda: True
        self.assertTrue(self.p.vault_available())

    def test_http_only_vault_is_never_writable(self):
        self.public()
        self.p.vault_http_readable = lambda: True
        with self.assertRaises(PermissionError):
            self.p.check_root(self.p.VAULT, writable=True)

    def test_probe_prefers_the_public_url_when_lan_is_dead(self):
        self.public('https://tunnel.example')
        tried = []

        def fake_probe(base):
            tried.append(base)
            return base.startswith('https://tunnel.example')

        self.p._probe_vault_route = fake_probe
        self.assertTrue(self.p.vault_http_readable())
        self.assertEqual(tried[0], 'https://tunnel.example')

    def test_probe_failure_is_not_read_as_an_empty_vault(self):
        self.public()
        self.p._probe_vault_route = lambda base: False
        self.assertFalse(self.p.vault_http_readable())
        self.assertFalse(self.p.vault_available())

    def test_endpoints_without_public_mode_are_lan_only(self):
        self.assertNotIn('https://tunnel.example', self.p.vault_read_endpoints())


class ProbeCaching(unittest.TestCase):
    """A reachability probe must not cost a network round trip on every call."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        vault = self.root / 'vault-ro'
        vault.mkdir()
        data = self.root / 'data'
        data.mkdir()
        (data / 'connection.json').write_text(
            json.dumps({'mode': 'public', 'public_url': 'https://tunnel.example'}), encoding='utf-8')
        conf = self.root / 'agent.conf'
        conf.write_text(json.dumps({
            'self': 'sondra', 'peer': 'eliza', 'token': 'test-token',
            'vault_local': str(vault), 'api_base': 'http://127.0.0.1:1',
        }), encoding='utf-8')
        self.p = load(conf)
        self.p.DATA_DIR = data
        self.calls = []
        self.answer = True
        def probe(base):
            self.calls.append(base)
            return self.answer
        self.p._probe_vault_route = probe

    def tearDown(self):
        self.tmp.cleanup()

    def test_repeated_calls_probe_once_within_the_ttl(self):
        self.assertTrue(self.p.vault_http_readable())
        self.assertTrue(self.p.vault_http_readable())
        self.assertTrue(self.p.vault_http_readable())
        self.assertEqual(len(self.calls), 1, f'probed {len(self.calls)} times, expected 1')

    def test_force_bypasses_the_cache(self):
        self.p.vault_http_readable()
        self.p.vault_http_readable(force=True)
        self.assertEqual(len(self.calls), 2)

    def test_expired_cache_probes_again(self):
        self.p.vault_http_readable()
        self.p._PROBE_CACHE['at'] -= (self.p.vault_probe_ttl() + 1)
        self.p.vault_http_readable()
        self.assertEqual(len(self.calls), 2)

    def test_a_negative_result_is_cached_too_but_recovers_after_ttl(self):
        # A negative answer legitimately tries EVERY endpoint before giving up,
        # so the first call costs one probe per endpoint. What the cache must
        # prevent is a second round on the next call.
        self.answer = False
        self.assertFalse(self.p.vault_http_readable())
        first_round = len(self.calls)
        self.assertEqual(first_round, len(self.p.vault_read_endpoints()))
        self.assertFalse(self.p.vault_http_readable())
        self.assertEqual(len(self.calls), first_round, 'cached negative must not re-probe')
        self.answer = True
        self.p._PROBE_CACHE['at'] -= (self.p.vault_probe_ttl() + 1)
        self.assertTrue(self.p.vault_http_readable())

    def test_ttl_is_configurable(self):
        import os
        os.environ['SKMR_VAULT_PROBE_TTL'] = '5'
        try:
            self.assertEqual(self.p.vault_probe_ttl(), 5)
        finally:
            os.environ.pop('SKMR_VAULT_PROBE_TTL', None)

    def test_mount_usable_is_its_own_predicate(self):
        self.assertFalse(self.p.vault_mount_usable())


class LocalVaultBacking(unittest.TestCase):
    """A single-machine installation has no SMB mount, and must still work.

    The is_mount() rule exists so an unmounted stub cannot pass as an empty
    vault. On a one-machine install there is no mount at all, so that rule left
    the vault permanently unwritable and the writer fell back to the HTTP route,
    which answered 401 -- measured during an installer probe. The backing is now
    declared in agent.conf. These cases pin both halves: a declared local vault
    carrying the Obsidian marker works, and everything the original rule caught
    is still caught.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        self.addCleanup(self.tmp.cleanup)

    def policy(self, backing=None, marker=True, actor='eliza'):
        if marker:
            (self.vault / '.obsidian').mkdir(exist_ok=True)
        conf = self.root / 'agent.conf'
        payload = {
            'self': actor, 'peer': 'sondra' if actor == 'eliza' else 'eliza',
            'token': 'test-token', 'vault_local': str(self.vault), 'vault_name': 'V',
            'api_base': 'http://127.0.0.1:1', 'api_lan': 'http://127.0.0.1:1',
            'vault_permissions': {'V': {'eliza': 'write', 'sondra': 'read'}},
        }
        if backing is not None:
            payload['vault_backing'] = backing
        conf.write_text(json.dumps(payload), encoding='utf-8')
        module = load(conf)
        module.DATA_DIR = self.root / 'data'
        # check_root re-reads the grant on every call, by design, so the
        # permission lookup has to keep resolving to THIS conf. Without this the
        # case graded itself against whatever agent.conf the machine happens to
        # have, and passed or failed for a reason of its own: as root it read a
        # conf that granted 'eliza' WRITE and passed, and as an ordinary user it
        # read a different one and failed on permissions, never reaching the
        # backing rule it exists to test.
        import os
        previous = os.environ.get('AGENTCOMM_CONF')
        os.environ['AGENTCOMM_CONF'] = str(conf)

        def restore():
            if previous is None:
                os.environ.pop('AGENTCOMM_CONF', None)
            else:
                os.environ['AGENTCOMM_CONF'] = previous

        self.addCleanup(restore)
        return module

    def test_default_backing_is_smb_so_existing_installs_are_unchanged(self):
        policy = self.policy()
        self.assertEqual(policy.vault_backing(), 'smb')
        with self.assertRaises(PermissionError) as caught:
            policy.check_root(self.vault, writable=True)
        self.assertIn('not mounted', str(caught.exception))

    def test_a_declared_local_vault_with_the_marker_is_writable(self):
        policy = self.policy(backing='local')
        self.assertEqual(policy.vault_backing(), 'local')
        policy.check_root(self.vault, writable=True)       # must not raise
        self.assertTrue(policy.vault_mount_usable())
        self.assertTrue(policy.vault_available())

    def test_a_local_vault_without_the_marker_is_refused(self):
        # The unmounted-stub case the original rule existed to catch.
        policy = self.policy(backing='local', marker=False)
        with self.assertRaises(PermissionError) as caught:
            policy.check_root(self.vault, writable=True)
        self.assertIn('.obsidian', str(caught.exception))
        self.assertFalse(policy.vault_mount_usable())

    def test_a_missing_local_vault_directory_is_refused(self):
        policy = self.policy(backing='local')
        missing = self.root / 'gone'
        policy.VAULT = missing
        with self.assertRaises(PermissionError) as caught:
            policy.check_root(missing, writable=True)
        self.assertIn('does not exist', str(caught.exception))

    def test_an_unknown_backing_falls_back_to_the_stricter_rule(self):
        policy = self.policy(backing='nfs-over-carrier-pigeon')
        self.assertEqual(policy.vault_backing(), 'smb')
        with self.assertRaises(PermissionError):
            policy.check_root(self.vault, writable=True)

    def test_local_backing_does_not_grant_write_to_a_read_agent(self):
        # Relaxing the physical rule must not touch the permission rule.
        policy = self.policy(backing='local', actor='sondra')
        with self.assertRaises(PermissionError) as caught:
            policy.check_root(self.vault, writable=True)
        self.assertIn('READ', str(caught.exception))
        policy.check_root(self.vault, writable=False)      # reading stays allowed

    def test_the_backing_is_re_read_so_a_change_applies_on_the_next_call(self):
        policy = self.policy(backing='local')
        self.assertEqual(policy.vault_backing(), 'local')
        conf_path = self.root / 'agent.conf'
        conf = json.loads(conf_path.read_text())
        conf['vault_backing'] = 'smb'
        conf_path.write_text(json.dumps(conf), encoding='utf-8')
        self.assertEqual(policy.vault_backing(), 'smb')


class DoctorVaultDiagnosis(unittest.TestCase):
    """Reported from the peer host: doctor called an HTTP-readable vault empty.

    Her mount dropped when the LAN went away. ``index.vault_available()`` looks
    only at the mount -- correctly, since indexing needs real files -- but doctor
    printed its False as "is empty or unreadable" while the authenticated route
    was serving that very vault. The three states must stay distinguishable, and
    "empty" must never be said about an unreachable one.
    """

    def setUp(self):
        sys.path.insert(0, '/root/.claude')
        sys.path.insert(0, '/root/.claude/lib')
        from skmr.commands import doctor_cmd
        self.doctor = doctor_cmd

    def _vault_row(self, *, mount, http, exists=True, non_empty=True):
        import contextlib
        import skmr_agent_policy as policy
        from skmr.memory.indexing import index
        root = Path('/mnt/sondra-vault-ro')
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(policy, 'vault_mount_usable', lambda: mount))
            stack.enter_context(patch.object(policy, 'vault_http_readable', lambda force=False: http))
            stack.enter_context(patch.object(index, 'vault_available', lambda: mount and non_empty))
            stack.enter_context(patch.object(index, 'vault_root', lambda: root))
            stack.enter_context(patch.object(Path, 'exists', lambda self: exists))
            return self.doctor._vault()

    def test_a_healthy_mount_is_ok(self):
        status, _ = self._vault_row(mount=True, http=True)
        self.assertEqual(status, self.doctor.OK)

    def test_http_only_is_a_warning_and_never_says_empty(self):
        status, detail = self._vault_row(mount=False, http=True)
        self.assertEqual(status, self.doctor.WARN)
        self.assertNotIn('empty', detail)
        self.assertIn('authenticated route', detail)
        self.assertIn('indexing', detail)

    def test_both_paths_down_fails_as_unreachable_not_empty(self):
        status, detail = self._vault_row(mount=False, http=False)
        self.assertEqual(status, self.doctor.FAIL)
        self.assertNotIn('empty', detail)
        self.assertIn('unreachable', detail)

    def test_a_missing_root_with_no_route_is_still_unreachable_not_empty(self):
        status, detail = self._vault_row(mount=False, http=False, exists=False)
        self.assertEqual(status, self.doctor.FAIL)
        self.assertNotIn('empty', detail)
        self.assertIn('unreachable', detail)

    def test_a_missing_root_that_is_http_readable_is_not_a_failure(self):
        # The mount point itself may be absent on a peer that never mounted it.
        status, _ = self._vault_row(mount=False, http=True, exists=False)
        self.assertEqual(status, self.doctor.WARN)

    def test_an_indexable_mount_that_is_genuinely_empty_is_not_called_reachable(self):
        # The one case where "nothing to read" is the truth must still be caught,
        # so widening the diagnosis did not delete the original check.
        status, _ = self._vault_row(mount=True, http=False, non_empty=False)
        self.assertEqual(status, self.doctor.FAIL)


if __name__ == '__main__':
    unittest.main(verbosity=1)
