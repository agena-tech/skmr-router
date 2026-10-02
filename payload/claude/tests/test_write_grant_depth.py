#!/usr/bin/env python3
"""A host must not be able to grant itself write access by editing its own file.

Local configuration is the weakest layer: whoever can write agent.conf can claim
any grant in it. When the canonical API is reachable it is the authority, so an
explicit disagreement -- server says read, local file says write -- must refuse.
An unreachable authority is not a denial: the local decision still stands, so a
network outage never blocks a legitimate writer.
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

LIB = Path('/root/.claude/lib/skmr_agent_policy.py')


def load(conf_path):
    previous = os.environ.get('AGENTCOMM_CONF')
    os.environ['AGENTCOMM_CONF'] = str(conf_path)
    try:
        spec = importlib.util.spec_from_file_location('policy_depth_test', LIB)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            os.environ.pop('AGENTCOMM_CONF', None)
        else:
            os.environ['AGENTCOMM_CONF'] = previous
    return module


class AuthoritativeGrant(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.vault = root / 'vault'
        self.vault.mkdir()
        (root / 'data').mkdir()
        self.conf_path = root / 'agent.conf'
        # The local file claims write for this agent.
        self.conf_path.write_text(json.dumps({
            'self': 'beta', 'peer': 'alpha', 'token': 't',
            'vault_local': str(self.vault), 'vault_name': 'Memory',
            'vault_permissions': {'Memory': {'beta': 'write', 'alpha': 'write'}},
            'api_base': 'http://127.0.0.1:1',
        }), encoding='utf-8')
        # The policy re-reads the grant file on every call, so the resolver must
        # point at this temp config for the whole test, not only during import.
        patcher = patch.dict(os.environ, {'AGENTCOMM_CONF': str(self.conf_path)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.p = load(self.conf_path)
        self.p.DATA_DIR = root / 'data'

    def tearDown(self):
        self.tmp.cleanup()

    def test_local_grant_alone_still_permits_when_no_authority_answers(self):
        self.p.authoritative_write_grant = lambda **kw: None
        self.assertTrue(self.p.may_write_vault())

    def test_authority_agreement_permits(self):
        self.p.authoritative_write_grant = lambda **kw: 'write'
        self.assertTrue(self.p.may_write_vault())

    def test_authority_disagreement_refuses_a_self_granted_write(self):
        self.p.authoritative_write_grant = lambda **kw: 'read'
        self.assertFalse(self.p.may_write_vault())

    def test_a_local_read_grant_is_never_upgraded_by_the_authority(self):
        conf = json.loads(self.conf_path.read_text())
        conf['vault_permissions']['Memory']['beta'] = 'read'
        self.conf_path.write_text(json.dumps(conf), encoding='utf-8')
        p = load(self.conf_path)
        p.DATA_DIR = self.p.DATA_DIR
        p.authoritative_write_grant = lambda **kw: 'write'
        self.assertFalse(p.may_write_vault())

    def test_check_root_refuses_a_write_the_authority_denies(self):
        # The grant decision comes before the physical mount checks, so this is
        # the message a self-granted writer gets even on a temp directory.
        self.p.authoritative_write_grant = lambda **kw: 'read'
        with self.assertRaises(PermissionError) as caught:
            self.p.check_root(self.p.VAULT, writable=True)
        self.assertIn('authoritative', str(caught.exception).lower())

    def test_reading_is_never_gated_by_the_authority(self):
        # A read may still fail on the mount here; what it must never fail on is
        # the write grant.
        self.p.authoritative_write_grant = lambda **kw: 'read'
        try:
            self.p.check_root(self.p.VAULT, writable=False)
        except PermissionError as error:
            self.assertNotIn('authoritative', str(error).lower())
            self.assertNotIn('read vault access', str(error).lower())

    def test_the_authority_answer_is_cached(self):
        calls = []

        def probe(base, token):
            calls.append(base)
            return 'write'

        self.p._probe_write_grant = probe
        self.p._GRANT_CACHE.update(at=0.0, value=None)
        self.p.authoritative_write_grant()
        self.p.authoritative_write_grant()
        self.assertEqual(len(calls), 1, f'probed {len(calls)} times, expected 1')

    def test_independent_vault_authority_is_separate_from_the_message_host(self):
        origin='http://127.0.0.1:8080'
        self.p.CONFIG.update(vault_origin=origin, storage_host=True,
                             transport_role='consumer', peer_api_lan='http://peer:8080')
        self.p._probe_write_grant=lambda base,token: 'write' if base==origin else 'read'
        self.assertEqual(self.p.authoritative_write_grant(force=True),'write')
        self.assertEqual(self.p.vault_read_endpoints(),[origin])


if __name__ == '__main__':
    unittest.main(verbosity=1)
