#!/usr/bin/env python3
"""No agent name may be baked into behaviour.

A fresh install names its agents whatever it likes. Anything that recognises
"eliza" or "sondra" specifically works on this machine and silently misbehaves on
the next one, so these tests assert the behaviour for arbitrary names and assert
that the historical names carry no special power.
"""
import ipaddress
import socket
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, '/root/.claude')
sys.path.insert(0, '/root/agentcomm')

from skmr.memory import intent  # noqa: E402
from skmr.agents import topology as T  # noqa: E402
import common as AC  # noqa: E402


def topo(*names):
    local = T.Agent(names[0], 'Commander', kind='local', vault_access='write')
    remote = [T.Agent(n, 'Lieutenant', kind='remote', host=f'host-{n}',
                      reports_to=names[0], vault_access='read') for n in names[1:]]
    return T.Topology(local=local, remote=remote)


class PeerIdentityFromTopology(unittest.TestCase):
    def test_an_arbitrary_peer_name_is_recognised(self):
        with patch.object(T, 'load', return_value=topo('Alpha', 'Beta')):
            result = intent.detect('Beta rolü ne?', allow_semantic=False)
        self.assertIsNotNone(result)
        self.assertEqual(result.intent, intent.IDENTITY_PEER)

    def test_a_historical_name_has_no_special_power(self):
        # With a topology that knows Alpha and Beta, "sondra" is just a word.
        with patch.object(T, 'load', return_value=topo('Alpha', 'Beta')):
            result = intent.detect('sondra rolü ne?', allow_semantic=False)
        if result is not None:
            self.assertNotEqual(result.intent, intent.IDENTITY_PEER,
                                'a name absent from the topology must not resolve as the peer')

    def test_the_installed_names_still_work_when_the_topology_holds_them(self):
        with patch.object(T, 'load', return_value=topo('Eliza', 'Sondra')):
            result = intent.detect('Sondra rolü ne?', allow_semantic=False)
        self.assertIsNotNone(result)
        self.assertEqual(result.intent, intent.IDENTITY_PEER)

    def test_no_agent_name_literal_remains_in_the_module(self):
        with open(intent.__file__, encoding='utf-8') as handle:
            source = handle.read().casefold()
        for name in ('sondra', 'eliza'):
            self.assertNotIn(f'"{name}"', source)
            self.assertNotIn(f"'{name}'", source)


class ColoursForAnyName(unittest.TestCase):
    def test_an_unknown_name_still_gets_a_colour(self):
        painted = AC.label('Gamma', color=True)
        self.assertIn('GAMMA:', painted)
        self.assertTrue(painted.startswith('\x1b['), f'no colour applied: {painted!r}')

    def test_distinct_names_get_distinct_colours(self):
        codes = {AC.agent_color(n) for n in ('Alpha', 'Beta', 'Gamma', 'Delta')}
        self.assertGreater(len(codes), 1)

    def test_the_same_name_is_always_the_same_colour(self):
        self.assertEqual(AC.agent_color('Alpha'), AC.agent_color('alpha'))

    def test_colour_is_never_applied_when_disabled(self):
        self.assertEqual(AC.label('Alpha', color=False), 'ALPHA:')

    def test_no_agent_name_literal_in_the_colour_table(self):
        with open(AC.__file__, encoding='utf-8') as handle:
            source = handle.read().casefold()
        self.assertNotIn('"sondra"', source)
        self.assertNotIn('"eliza"', source)


class SelfEndpointDetection(unittest.TestCase):
    def setUp(self):
        from skmr.commands import assign_role_cmd
        self.mod = assign_role_cmd

    def local_ip(self):
        for address in self.mod.local_addresses():
            try:
                parsed = ipaddress.ip_address(address)
            except ValueError:
                continue
            if not parsed.is_loopback:
                return address
        return None

    def test_a_loopback_endpoint_is_recognised_as_this_machine(self):
        self.assertTrue(self.mod.is_local_endpoint('127.0.0.1'))
        self.assertTrue(self.mod.is_local_endpoint('localhost'))
        self.assertTrue(self.mod.is_local_endpoint('::1'))

    def test_this_machines_own_lan_address_is_recognised_too(self):
        address = self.local_ip()
        if address is None:
            self.skipTest('no non-loopback local address to test with')
        self.assertTrue(self.mod.is_local_endpoint(address),
                        f'{address} is an address of this machine and must count as local')

    def test_a_remote_address_is_not_local(self):
        self.assertFalse(self.mod.is_local_endpoint('203.0.113.7'))


class EndpointSelfIdentification(unittest.TestCase):
    """Addressing cannot answer "is this me"; the service can.

    Behind NAT or a WSL port proxy a machine's own public address is not an
    address of its interfaces, so an endpoint written that way loops back to this
    very server while every address test says it is remote. Asking the endpoint
    who it is settles it: the administrative secret must never be posted to our
    own API.
    """

    def setUp(self):
        from skmr.commands import assign_role_cmd
        self.mod = assign_role_cmd

    def test_an_endpoint_reporting_our_own_identity_is_refused(self):
        with patch.object(self.mod, '_endpoint_identity', return_value='alpha'):
            with self.assertRaises(ValueError) as caught:
                self.mod.reject_self_endpoint('https://tunnel.example', {'self': 'Alpha'})
        self.assertIn('this machine', str(caught.exception).lower())

    def test_an_endpoint_reporting_the_peer_is_accepted(self):
        with patch.object(self.mod, '_endpoint_identity', return_value='beta'):
            self.mod.reject_self_endpoint('https://tunnel.example', {'self': 'alpha'})

    def test_an_unreachable_endpoint_is_not_rejected_on_identity_grounds(self):
        with patch.object(self.mod, '_endpoint_identity', return_value=None):
            self.mod.reject_self_endpoint('https://tunnel.example', {'self': 'alpha'})

    def test_identity_comparison_ignores_case(self):
        with patch.object(self.mod, '_endpoint_identity', return_value='ALPHA'):
            with self.assertRaises(ValueError):
                self.mod.reject_self_endpoint('https://tunnel.example', {'self': 'alpha'})


if __name__ == '__main__':
    unittest.main(verbosity=1)
