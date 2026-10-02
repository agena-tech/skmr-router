#!/usr/bin/env python3
"""LAN addressing is configured; the public tunnel is never configuration.

A remote agent's LAN address is durable: it belongs to the assignment and is
written where the transport reads it. A quick-tunnel URL is the opposite -- it
changes every time the tunnel restarts, so persisting one produces config that
is wrong by the next session. The operator is never asked for it; the host opens
a tunnel only when the LAN cannot carry the link.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, '/root/.claude')
sys.path.insert(0, '/root/agentcomm')
from skmr.agents import topology as T  # noqa: E402
from skmr.commands import assign_role_cmd as A  # noqa: E402


class AgentCarriesItsLan(unittest.TestCase):
    def test_lan_defaults_to_empty(self):
        self.assertEqual(T.Agent('Alpha', 'Commander').lan, '')

    def test_an_ip_is_accepted(self):
        self.assertEqual(T.validate_host('192.168.1.102'), '192.168.1.102')

    def test_a_hostname_is_accepted(self):
        self.assertEqual(T.validate_host('laptop-x'), 'laptop-x')

    def test_a_malformed_address_is_rejected(self):
        for bad in ('999.1.1.1', 'a b', '::zz'):
            with self.subTest(bad=bad), self.assertRaises(T.ValidationError):
                T.validate_host(bad)

    def test_lan_survives_a_save_load_round_trip(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        target = Path(tmp.name) / 'agents.json'
        with patch.object(T, 'path', lambda: target):
            T.save(T.Topology(
                local=T.Agent('Alpha', 'Commander', kind='local', vault_access='write', lan='10.0.0.1'),
                remote=[T.Agent('Beta', 'Lieutenant', kind='remote', host='box-b',
                                reports_to='Alpha', vault_access='read', lan='192.168.1.102')]))
            loaded = T.load()
        self.assertEqual(loaded.local.lan, '10.0.0.1')
        self.assertEqual(loaded.remote[0].lan, '192.168.1.102')
        self.assertIn('lan', json.loads(target.read_text())['local'])


class TunnelUrlIsNotConfiguration(unittest.TestCase):
    def test_the_admin_endpoint_prefers_the_live_public_url(self):
        conf = {'self': 'alpha', 'peer': 'beta', 'listen_port': 8080,
                'peer_api_lan': 'http://192.168.1.102:8080'}
        state = {'mode': 'public', 'public_url': 'https://tunnel.example', 'transport_role': 'consumer'}
        self.assertEqual(A.peer_admin_endpoint(conf, state), 'https://tunnel.example')

    def test_it_falls_back_to_the_configured_lan(self):
        conf = {'self': 'alpha', 'peer': 'beta', 'listen_port': 8080,
                'peer_api_lan': 'http://192.168.1.102:8080'}
        self.assertEqual(A.peer_admin_endpoint(conf, {'mode': 'private'}),
                         'http://192.168.1.102:8080')

    def test_a_stale_persisted_tunnel_url_is_ignored(self):
        # The whole point: a URL written into agent.conf by an earlier session
        # must never be used, because it expired with that session's tunnel.
        conf = {'self': 'alpha', 'peer': 'beta', 'listen_port': 8080,
                'peer_api_lan': 'http://192.168.1.102:8080',
                'peer_admin_url': 'https://expired-tunnel.example'}
        self.assertEqual(A.peer_admin_endpoint(conf, {'mode': 'private'}),
                         'http://192.168.1.102:8080')

    def test_the_hosts_own_tunnel_is_never_taken_for_the_peer(self):
        # On the host, public_url is OUR tunnel. Using it would address ourselves
        # while the peer sits behind the LAN address.
        conf = {'self': 'alpha', 'peer': 'beta', 'listen_port': 8080,
                'peer_api_lan': 'http://192.168.1.102:8080'}
        state = {'mode': 'public', 'public_url': 'https://my-own-tunnel.example',
                 'transport_role': 'host'}
        self.assertEqual(A.peer_admin_endpoint(conf, state), 'http://192.168.1.102:8080')

    def test_the_consumer_does_use_the_public_url(self):
        conf = {'self': 'beta', 'peer': 'alpha', 'listen_port': 8080,
                'peer_api_lan': 'http://192.168.1.110:8080'}
        state = {'mode': 'public', 'public_url': 'https://host-tunnel.example',
                 'transport_role': 'consumer'}
        self.assertEqual(A.peer_admin_endpoint(conf, state), 'https://host-tunnel.example')

    def test_no_lan_and_no_tunnel_means_no_endpoint(self):
        self.assertIsNone(A.peer_admin_endpoint({'self': 'alpha'}, {'mode': 'private'}))


class SelfGuardUsesLocalityNotApiBase(unittest.TestCase):
    """api_base means different things per role, so it cannot answer "is this me".

    On a consumer, api_base historically points at the PEER. Comparing the
    resolved peer endpoint against it made the guard reject the correct address as
    "my own API" and broke --sync-peer entirely. Locality is decided by the
    machine's own addresses and by the service's identity, never by that field.
    """

    def test_a_consumer_whose_api_base_is_the_peer_can_still_sync(self):
        conf = {'self': 'beta', 'peer': 'alpha', 'listen_port': 8080,
                'api_base': 'http://192.168.1.110:8080',      # the PEER, on a consumer
                'api_lan': 'http://192.168.1.102:8080',       # this machine
                'peer_api_lan': 'http://192.168.1.110:8080',
                'config_admin_token': 'secret'}
        with patch.object(A, '_endpoint_identity', return_value='alpha'):
            route = A._sync_endpoint(conf)
        self.assertEqual(route, 'http://192.168.1.110:8080/api/config/roles')

    def test_an_endpoint_equal_to_our_own_lan_is_still_refused(self):
        conf = {'self': 'alpha', 'peer': 'beta', 'listen_port': 8080,
                'api_lan': 'http://192.168.1.110:8080',
                'peer_api_lan': 'http://192.168.1.110:8080',
                'config_admin_token': 'secret'}
        with self.assertRaises(ValueError):
            A._sync_endpoint(conf)


class LanFlowsIntoTheTransportConfig(unittest.TestCase):
    def setUp(self):
        import roles
        self.roles = roles
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_assignment_writes_both_lan_addresses_into_conf(self):
        conf = {'self': 'alpha', 'peer': 'beta', 'token': 't',
                'tokens': {'alpha': 'a', 'beta': 'b'}, 'listen_port': 8080,
                'vault_name': 'Memory', 'smb_vault_share': 'Memory'}
        payload = {
            'agents': [
                {'name': 'Alpha', 'role': 'Commander', 'lan': '10.0.0.1'},
                {'name': 'Beta', 'role': 'Lieutenant', 'host': 'box-b',
                 'reports_to': 'Alpha', 'lan': '192.168.1.102'},
            ],
            'vault_permissions': {'Memory': {'alpha': 'write', 'beta': 'read'}},
            'host_roles': {'alpha': 'host', 'beta': 'consumer'},
        }
        updated = self.roles.transport_addresses(conf, payload['agents'], 'alpha', 'beta')
        self.assertEqual(updated['api_lan'], 'http://10.0.0.1:8080')
        self.assertEqual(updated['peer_api_lan'], 'http://192.168.1.102:8080')

    def test_a_missing_lan_leaves_the_existing_value_alone(self):
        conf = {'self': 'alpha', 'peer': 'beta', 'listen_port': 8080,
                'api_lan': 'http://10.0.0.9:8080', 'peer_api_lan': 'http://10.0.0.8:8080'}
        agents = [{'name': 'Alpha', 'role': 'C'}, {'name': 'Beta', 'role': 'L'}]
        updated = self.roles.transport_addresses(conf, agents, 'alpha', 'beta')
        self.assertEqual(updated['api_lan'], 'http://10.0.0.9:8080')
        self.assertEqual(updated['peer_api_lan'], 'http://10.0.0.8:8080')

    def test_a_persisted_tunnel_url_is_dropped_from_conf(self):
        conf = {'self': 'alpha', 'peer': 'beta', 'listen_port': 8080,
                'peer_admin_url': 'https://expired.example'}
        agents = [{'name': 'Alpha', 'role': 'C', 'lan': '10.0.0.1'},
                  {'name': 'Beta', 'role': 'L', 'lan': '10.0.0.2'}]
        updated = self.roles.transport_addresses(conf, agents, 'alpha', 'beta')
        self.assertNotIn('peer_admin_url', updated)


if __name__ == '__main__':
    unittest.main(verbosity=1)
