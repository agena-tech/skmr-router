#!/usr/bin/env python3
"""Vault access is an assigned permission, not a hardcoded identity.

The operating rules under test:
  * every agent carries read or write access to the vault,
  * both agents may be write; both may NOT be read -- some agent must be able
    to write permanent memory or the store can never grow,
  * write access is what grants task creation and SMB sharing,
  * an unassigned permission is READ, never write: the default must fail closed.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, '/root/.claude')
from skmr.agents import topology as T  # noqa: E402


def agent(name, role='Agent', kind='local', access='write', **kw):
    return T.Agent(name=name, role=role, kind=kind, vault_access=access, **kw)


class AccessValues(unittest.TestCase):
    def test_read_and_write_are_the_only_values(self):
        self.assertEqual(T.validate_vault_access('read'), 'read')
        self.assertEqual(T.validate_vault_access('WRITE'), 'write')
        for bad in ('admin', 'rw', 'none', 'write-only', '  '):
            with self.subTest(bad=bad), self.assertRaises(T.ValidationError):
                T.validate_vault_access(bad)

    def test_unassigned_access_is_read_not_write(self):
        self.assertFalse(T.Agent(name='X', role='R').can_write_vault())
        self.assertEqual(T.Agent(name='X', role='R').access(), 'read')


class TopologyRules(unittest.TestCase):
    def test_write_and_read_pair_is_valid(self):
        t = T.Topology(local=agent('Alpha', access='write'),
                       remote=[agent('Beta', kind='remote', access='read',
                                     host='host-b', reports_to='Alpha')])
        self.assertIs(T.validate(t), t)

    def test_both_write_is_allowed(self):
        t = T.Topology(local=agent('Alpha', access='write'),
                       remote=[agent('Beta', kind='remote', access='write',
                                     host='host-b', reports_to='Alpha')])
        self.assertIs(T.validate(t), t)

    def test_both_read_is_rejected(self):
        t = T.Topology(local=agent('Alpha', access='read'),
                       remote=[agent('Beta', kind='remote', access='read',
                                     host='host-b', reports_to='Alpha')])
        with self.assertRaises(T.ValidationError) as caught:
            T.validate(t)
        self.assertIn('write', str(caught.exception).lower())

    def test_a_lone_read_only_agent_is_rejected(self):
        with self.assertRaises(T.ValidationError):
            T.validate(T.Topology(local=agent('Alpha', access='read')))

    def test_writers_are_listed(self):
        t = T.Topology(local=agent('Alpha', access='write'),
                       remote=[agent('Beta', kind='remote', access='read',
                                     host='host-b', reports_to='Alpha')])
        self.assertEqual([a.name for a in t.writers()], ['Alpha'])


class DerivedCapabilities(unittest.TestCase):
    def test_write_access_grants_tasks_and_smb_sharing(self):
        writer = agent('Alpha', access='write')
        self.assertTrue(writer.can_write_vault())
        self.assertTrue(writer.can_create_tasks())
        self.assertTrue(writer.can_share_smb())

    def test_read_access_grants_neither(self):
        reader = agent('Beta', access='read')
        self.assertFalse(reader.can_write_vault())
        self.assertFalse(reader.can_create_tasks())
        self.assertFalse(reader.can_share_smb())


class Persistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name) / 'agents.json'
        self._path = T.path
        T.path = lambda: self.file

    def tearDown(self):
        T.path = self._path
        self.tmp.cleanup()

    def test_access_survives_a_save_load_round_trip(self):
        T.save(T.Topology(local=agent('Alpha', access='write'),
                          remote=[agent('Beta', kind='remote', access='read',
                                        host='host-b', reports_to='Alpha')]))
        loaded = T.load()
        self.assertEqual(loaded.local.access(), 'write')
        self.assertEqual(loaded.remote[0].access(), 'read')
        self.assertIn('vault_access', json.loads(self.file.read_text())['local'])

    def test_a_legacy_file_without_access_loads_as_read(self):
        self.file.write_text(json.dumps({
            'local': {'name': 'Alpha', 'role': 'Commander', 'kind': 'local'},
            'remote': [], 'updated_at': '',
        }), encoding='utf-8')
        self.assertEqual(T.load().local.access(), 'read')

    def test_an_unknown_key_in_a_stored_agent_does_not_crash_the_load(self):
        self.file.write_text(json.dumps({
            'local': {'name': 'Alpha', 'role': 'R', 'kind': 'local', 'future_field': 1},
            'remote': [], 'updated_at': '',
        }), encoding='utf-8')
        self.assertEqual(T.load().local.name, 'Alpha')


if __name__ == '__main__':
    unittest.main(verbosity=1)
