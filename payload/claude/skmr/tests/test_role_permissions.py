"""Isolated assignment contracts; no reads/writes of live /root state."""
import importlib
import json
import os
import pathlib
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

RESULT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(RESULT / '.claude'))
import skmr.agents
import skmr.commands
skmr.agents.__path__.insert(0, str(RESULT / '.claude/skmr/agents'))
skmr.commands.__path__.insert(0, str(RESULT / '.claude/skmr/commands'))
sys.path[:0] = [str(RESULT / '.claude/lib'), str(RESULT / 'agentcomm')]
from skmr.agents import topology
from skmr.commands import assign_role_cmd as command
from skmr.core import config


class RolePermissions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.paths = {key: str(self.root / filename) for key, filename in
                      [('agents_path', 'agents.json'), ('memory_path', 'MEMORY.md'),
                       ('claude_md', 'CLAUDE.md'), ('state_dir', 'state')]}
        self.confpath = self.root / 'agent.conf'
        self.conf = {'self': 'eliza', 'peer': 'sondra', 'token': 'retain-me',
                     'tokens': {'eliza': 'one', 'sondra': 'two'}, 'smb_vault_share': 'Memory',
                     'transport_role': 'host', 'vault_permissions': {'Memory': {'eliza': 'write', 'sondra': 'read'}}}
        self.confpath.write_text(json.dumps(self.conf))
        env = patch.dict(os.environ, {'AGENTCOMM_CONF': str(self.confpath),
                         'SKMR_AGENTS_PATH': self.paths['agents_path'], 'AGENTCOMM_LIB': str(RESULT / '.claude/lib')})
        env.start(); self.addCleanup(env.stop)
        cache = patch.object(config, '_cache', dict(config.DEFAULTS, **self.paths))
        cache.start(); self.addCleanup(cache.stop)
        self.initial = topology.Topology(
            local=topology.Agent('Eliza', 'Commander', host='DESKTOP-TGQ9B9L', vault_access='write'),
            remote=[topology.Agent('Sondra', 'Lieutenant', host='LAPTOP-BHM2J6BL', reports_to='Eliza', kind='remote', vault_access='read')])
        topology.save(self.initial)
        pathlib.Path(self.paths['claude_md']).write_text('Keep instructions.\n')
        pathlib.Path(self.paths['memory_path']).write_text('# Working continuity\n\n## Manual notes\n\nKeep notes.\n')

    def module(self, name):
        self.assertTrue((RESULT / ('agentcomm/roles.py' if name == 'roles' else '.claude/lib/skmr_permissions.py')).exists(), f'{name} interface missing')
        return importlib.import_module(name)

    def payload(self):
        return {'agents': [dict(name='Eliza', role='Commander', host='DESKTOP-TGQ9B9L', reports_to='', vault_access='write'),
                           dict(name='Sondra', role='Lieutenant', host='LAPTOP-BHM2J6BL', reports_to='Eliza', vault_access='read')],
                'vault_permissions': {'Memory': {'ELIZA': 'write', 'Sondra': 'read'}},
                'host_roles': {'Eliza': 'host', 'Sondra': 'consumer'}}

    def test_permissions_fail_closed_and_ignore_rank(self):
        helper = self.module('skmr_permissions')
        self.assertEqual(helper.permission('COMMANDER', conf={'self': 'Commander', 'rank': 'Commander'}), 'read')
        self.assertTrue(helper.can_write('ELIZA', conf=self.conf))
        self.assertFalse(helper.can_write('sondra', conf=self.conf))
        self.assertFalse(helper.can_write('eliza', vault='Other', conf=self.conf))
        self.assertEqual(helper.config_path(), self.confpath)

    def test_permission_validation_covers_every_vault_and_identity(self):
        helper = self.module('skmr_permissions')
        self.assertRaises(ValueError, helper.validate_permissions, {'Memory': {'eliza': 'read', 'sondra': 'read'}}, ['eliza', 'sondra'])
        self.assertRaises(ValueError, helper.validate_permissions, {'Memory': {'eliza': 'write'}, 'Other': {'sondra': 'read'}}, ['eliza', 'sondra'])
        self.assertRaises(ValueError, helper.validate_permissions, {'Memory': {'eliza': 'admin'}}, ['eliza', 'sondra'])
        self.assertRaises(ValueError, helper.validate_permissions, {'Memory': {'intruder': 'write'}}, ['eliza', 'sondra'])
        self.assertEqual(helper.validate_permissions({'Memory': {'ELIZA': 'write', 'sondra': 'write'}}, ['eliza', 'sondra'])['Memory']['eliza'], 'write')

    def test_cli_preserves_identity_hosts_relationships_and_remote(self):
        self.assertEqual(command.run(['--non-interactive', '--local-only', '--role', 'Commander'], None), 0)
        current = topology.load()
        self.assertEqual(current.local.name, 'Eliza')
        self.assertEqual(current.local.host, 'DESKTOP-TGQ9B9L')
        self.assertEqual(current.remote[0].reports_to, 'Eliza')
        self.assertEqual(current.remote[0].host, 'LAPTOP-BHM2J6BL')
        self.assertEqual(current.local.vault_access, 'write')
        self.assertIn('Keep instructions.', pathlib.Path(self.paths['claude_md']).read_text())

    def test_apply_roles_mirrors_self_and_keeps_auth_config(self):
        roles = self.module('roles')
        self.conf['self'], self.conf['peer'] = 'sondra', 'eliza'
        self.confpath.write_text(json.dumps(self.conf))
        roles.apply_roles(self.payload())
        current = topology.load()
        self.assertEqual(current.local.name, 'Sondra')
        self.assertEqual(current.local.reports_to, 'Eliza')
        self.assertEqual(current.remote[0].name, 'Eliza')
        persisted = json.loads(self.confpath.read_text())
        self.assertEqual(persisted['token'], 'retain-me')
        self.assertEqual(persisted['self'], 'sondra')
        self.assertEqual(persisted['transport_role'], 'consumer')

    def test_reject_incomplete_roster_and_rename_before_any_write(self):
        roles = self.module('roles')
        original = self.confpath.read_bytes()
        payload = self.payload(); payload['agents'] = payload['agents'][:1]
        self.assertRaises(ValueError, roles.apply_roles, payload)
        payload = self.payload(); payload['agents'][0]['name'] = 'Other'
        self.assertRaises(ValueError, roles.apply_roles, payload)
        self.assertEqual(self.confpath.read_bytes(), original)

    def test_apply_roles_rolls_back_conf_when_agents_replace_fails(self):
        roles = self.module('roles')
        before = self.confpath.read_bytes()
        real = os.replace
        def fail_agents(src, dst):
            if str(dst) == self.paths['agents_path']:
                raise OSError('simulated disk failure')
            return real(src, dst)
        with patch.object(roles.os, 'replace', side_effect=fail_agents):
            self.assertRaises(OSError, roles.apply_roles, self.payload())
        self.assertEqual(self.confpath.read_bytes(), before)

    def test_cli_accepts_write_write_and_named_vault(self):
        self.assertEqual(command.run(['--non-interactive', '--local-only', '--vault', 'Second', '--local-access', 'write', '--remote-access', 'write'], None), 0)
        persisted = json.loads(self.confpath.read_text())
        self.assertEqual(persisted['vault_permissions']['Second'], {'eliza': 'write', 'sondra': 'write'})
        self.assertEqual(topology.load().remote[0].vault_access, 'read')
        self.assertEqual(topology.load().remote[0].vault_permissions['Second'], 'write')

    def test_cli_rolls_back_every_store_on_memory_failure(self):
        before = {p: pathlib.Path(p).read_bytes() for p in [str(self.confpath), self.paths['agents_path'], self.paths['memory_path'], self.paths['claude_md']]}
        with patch.object(command.native, 'mutate', side_effect=OSError('disk unavailable')):
            status = command.run(['--non-interactive', '--local-only', '--local-access', 'read', '--remote-access', 'write'], None)
        self.assertEqual(status, 1)
        for p, contents in before.items(): self.assertEqual(pathlib.Path(p).read_bytes(), contents)

    def test_permission_edit_requires_peer_sync_or_rolls_back(self):
        before = self.confpath.read_bytes()
        result = command.run(['--non-interactive', '--local-access', 'read', '--remote-access', 'write'], None)
        self.assertEqual(result, 1)
        self.assertEqual(self.confpath.read_bytes(), before)

    def test_unknown_option_and_missing_access_are_rejected(self):
        before = self.confpath.read_bytes()
        self.assertEqual(command.run(['--non-interactive', '--local-only', '--local-access', '--remote-access', 'read'], None), 2)
        self.assertEqual(command.run(['--non-interactive', '--local-only', '--wriet', 'yes'], None), 2)
        self.assertEqual(self.confpath.read_bytes(), before)

    def test_api_updates_docs_and_active_transport_in_same_transaction(self):
        roles = self.module('roles')
        statepath = self.root / 'data/connection.json'
        statepath.parent.mkdir()
        statepath.write_text(json.dumps({'mode': 'public', 'public_url': 'https://peer.example', 'transport_role': 'host'}))
        payload = self.payload(); payload['host_roles'] = {'eliza': 'consumer', 'sondra': 'host'}
        roles.apply_roles(payload)
        self.assertEqual(json.loads(statepath.read_text())['transport_role'], 'host')
        self.assertEqual(json.loads(self.confpath.read_text())['transport_role'], 'consumer')
        self.assertIn('Eliza — Commander', pathlib.Path(self.paths['memory_path']).read_text())
        self.assertIn('SKMR_AGENT_TOPOLOGY_START', pathlib.Path(self.paths['claude_md']).read_text())
        self.assertIn('Keep instructions.', pathlib.Path(self.paths['claude_md']).read_text())
        self.assertEqual(json.loads(statepath.read_text())['public_url'], 'https://peer.example')

    def test_actual_peer_sync_sends_admin_secret_and_complete_payload(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(handler):
                received.append((handler.path, dict(handler.headers), json.loads(handler.rfile.read(int(handler.headers['Content-Length'])))))
                handler.send_response(200); handler.end_headers()
                handler.wfile.write(json.dumps({'ok': True, 'self': 'sondra'}).encode())
            def log_message(self, *args): pass
        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        # The peer endpoint now comes from the durable LAN address; a tunnel URL
        # is never persisted, so there is no peer_admin_url to set.
        self.conf.update(config_admin_token='separate-admin-secret', peer_api_lan=f'http://127.0.0.1:{server.server_port}')
        self.confpath.write_text(json.dumps(self.conf))
        self.assertEqual(command.run(['--non-interactive', '--local-access', 'read', '--remote-access', 'write'], None), 0)
        route, headers, payload = received[0]
        self.assertEqual(route, '/api/config/roles')
        self.assertEqual(headers['X-Config-Token'], 'separate-admin-secret')
        self.assertNotIn('Authorization', headers)
        self.assertEqual(payload['vault_permissions']['Memory'], {'eliza': 'read', 'sondra': 'write'})

    def test_transport_host_rejects_self_endpoint(self):
        self.assertRaises(ValueError, command._sync_endpoint, dict(self.conf, peer_api_lan='http://127.0.0.1:8080'))
        self.assertRaises(ValueError, command._sync_endpoint, dict(self.conf, api_lan='http://192.168.1.110:8080', peer_api_lan='http://192.168.1.110:8080'))

    def test_a_persisted_tunnel_url_cannot_route_the_admin_secret(self):
        # Regression for the model change: an expired URL left in agent.conf by an
        # earlier session must not be reachable through configuration at all.
        conf = dict(self.conf, peer_admin_url='https://expired-tunnel.example')
        conf.pop('peer_api_lan', None)
        self.assertRaises(ValueError, command._sync_endpoint, conf)

    def test_topology_rejects_invalid_explicit_grant_and_second_vault_without_writer(self):
        self.initial.remote[0].vault_access = 'admin'
        self.assertRaises(ValueError, topology.validate, self.initial)
        self.initial.remote[0].vault_access = 'read'
        self.initial.local.vault_permissions = {'Second': 'read'}
        self.assertRaises(ValueError, topology.validate, self.initial)

    def test_interactive_asks_each_existing_vault_for_each_agent(self):
        self.conf['vault_permissions']['Second'] = {'eliza': 'write', 'sondra': 'read'}
        self.confpath.write_text(json.dumps(self.conf))
        prompts = []
        def answer(prompt, default=''):
            prompts.append(prompt)
            return default
        with patch.object(command, '_interactive', return_value=True), patch.object(command, '_ask', side_effect=answer):
            self.assertEqual(command.run(['--local-only'], None), 0)
        permission_prompts = [prompt for prompt in prompts if 'onlyread' in prompt]
        self.assertEqual(len(permission_prompts), 4)

    def test_invalid_host_roster_and_writerless_payload_never_persist(self):
        roles = self.module('roles')
        before = self.confpath.read_bytes()
        payload = self.payload(); payload['host_roles']['Sondra'] = 'host'
        self.assertRaises(ValueError, roles.apply_roles, payload)
        payload = self.payload(); payload['vault_permissions']['Memory'] = {'eliza': 'read', 'sondra': 'read'}
        self.assertRaises(ValueError, roles.apply_roles, payload)
        self.assertEqual(self.confpath.read_bytes(), before)

    def test_invalid_instruction_delimiters_roll_back_all_api_stores(self):
        roles = self.module('roles')
        claude = pathlib.Path(self.paths['claude_md'])
        claude.write_text('Keep instructions.\n<!-- SKMR_AGENT_TOPOLOGY_END -->\n<!-- SKMR_AGENT_TOPOLOGY_START -->\n')
        paths = [self.confpath, pathlib.Path(self.paths['agents_path']), pathlib.Path(self.paths['memory_path']), claude]
        before = {p: p.read_bytes() for p in paths}
        self.assertRaises(ValueError, roles.apply_roles, self.payload())
        for path, data in before.items(): self.assertEqual(path.read_bytes(), data)
        self.assertFalse((self.root / 'data/connection.json').exists())

    def test_duplicate_casefold_grants_fail_closed(self):
        helper = self.module('skmr_permissions')
        conf = dict(self.conf, vault_permissions={'Memory': {'Eliza': 'write', 'ELIZA': 'write'}})
        self.assertFalse(helper.can_write('eliza', conf=conf))
        self.assertRaises(ValueError, helper.validate_permissions, conf['vault_permissions'], ['eliza', 'sondra'])


if __name__ == '__main__': unittest.main()
