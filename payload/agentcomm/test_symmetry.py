"""Agent capabilities and host selection must not depend on Eliza/Sondra names."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import common
import server as S
import send as C
from fastapi.testclient import TestClient


class Symmetry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'messages').mkdir()
        (self.root / 'data').mkdir()
        self.vault = self.root / 'vault'; self.vault.mkdir()
        self.conf = {'self': 'alpha', 'peer': 'beta', 'token': 'alpha-token',
            'tokens': {'alpha': 'alpha-token', 'beta': 'beta-token'},
            'vault_local': str(self.vault), 'vault_name': 'Memory',
            'vault_permissions': {'Memory': {'alpha': 'read', 'beta': 'write'}},
            'transport_role': 'consumer', 'storage_host': True,
            'config_admin_token': 'admin-fixture', 'api_base': 'http://127.0.0.1:1'}
        self.conf['writer'] = str(Path(__file__).parent.parent/'.claude/skills/skmr/scripts/obsidian_memory.py')
        self.patches = [patch.object(S, 'CONF', self.conf),
            patch.object(S, 'MSG_DIR', self.root/'messages'),
            patch.object(S, 'DATA_DIR', self.root/'data'),
            patch.object(S, 'VAULT_ROOT', self.vault),
            patch.object(C, 'CONF', self.conf), patch.object(C, 'SELF', 'alpha'),
            patch.object(C, 'PEER', 'beta'), patch.object(common,'BASE_DIR',self.root),
            patch.object(C, 'CONNECTION_STATE', self.root/'data/connection.json')]
        for p in self.patches: p.start()
        self.client = TestClient(S.app)

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.tmp.cleanup()

    def post(self, path, actor, **body):
        return self.client.post(path, json=body, headers={'X-Agent-Token': actor+'-token'})

    def test_custom_names_send_both_directions_without_path_escape(self):
        for actor, peer in [('alpha','beta'), ('beta','alpha')]:
            r = self.post('/api/message/', actor, **{'from':actor,'to':peer,'text':'hello'})
            self.assertEqual(r.status_code, 200, r.text)
        for who in ('../escape', 'unknown'):
            r=self.post('/api/message/','alpha', **{'from':'alpha','to':who,'text':'bad'})
            self.assertIn(r.status_code, (403,422))

    def test_writer_peer_can_allocate_task_even_when_local_host_is_reader(self):
        with patch.object(S,'_next_task_number',return_value=9), patch.object(S,'_write_task'):
            r=self.post('/api/create/','beta', **{'from':'beta','to':'alpha','text':'work'})
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(r.json()['task_no'],9)

    def test_reader_cannot_allocate_task_even_with_commander_name(self):
        self.conf.update(self='eliza',peer='sondra',tokens={'eliza':'eliza-token','sondra':'sondra-token'})
        self.conf['vault_permissions']={'Memory':{'eliza':'read','sondra':'write'}}
        with patch.object(S,'_next_task_number',return_value=1),patch.object(S,'_write_task'):
            r=self.post('/api/create/','eliza', **{'from':'eliza','to':'sondra','text':'denied'})
        self.assertEqual(r.status_code,403,r.text)
        self.assertEqual(list((self.root/'messages').iterdir()),[])

    def test_both_write_can_create_regardless_host_or_rank(self):
        self.conf['vault_permissions']['Memory']['alpha']='write'
        with patch.object(S,'_next_task_number',return_value=4),patch.object(S,'_write_task'):
            for actor,peer in [('alpha','beta'),('beta','alpha')]:
                r=self.post('/api/create/',actor, **{'from':actor,'to':peer,'text':'work'})
                self.assertEqual(r.status_code,200,r.text)

    def test_task_retry_after_host_switch_uses_same_storage_receipt(self):
        body={'from':'beta','to':'alpha','text':'same work','request_id':'host-retry-1234'}
        with patch.object(S,'_next_task_number',side_effect=[7,8]) as allocate,patch.object(S,'_write_task'):
            first=self.post('/api/storage/task','beta',**body)
            second=self.post('/api/create/','beta',**body)
        self.assertEqual(first.status_code,200,first.text)
        self.assertEqual(second.status_code,200,second.text)
        self.assertEqual(second.json()['task_no'],7)
        self.assertEqual(allocate.call_count,1)

    def test_message_token_cannot_edit_permissions(self):
        r=self.client.post('/api/config/roles',json={},headers={'X-Config-Token':'alpha-token'})
        self.assertEqual(r.status_code,401,r.text)

    def test_local_cli_revocation_is_enforced_without_service_restart(self):
        conf_path=self.root/'agent.conf'
        conf_path.write_text(json.dumps(self.conf))
        with patch.object(S,'_LOADED_CONF',self.conf),patch.object(common,'CONF_PATH',conf_path),patch.object(S,'_CONFIG_STAMP',0):
            self.assertEqual(self.client.get('/api/health/').status_code,200)
            changed=dict(self.conf,vault_permissions={'Memory':{'alpha':'write','beta':'read'}})
            conf_path.write_text(json.dumps(changed))
            r=self.post('/api/create/','beta',**{'from':'beta','to':'alpha','text':'revoked'})
            self.assertEqual(r.status_code,403,r.text)
        self.assertEqual(list((self.root/'messages').iterdir()),[])

    def test_consumer_can_become_public_host_without_renaming(self):
        with patch.object(C,'_cloudflared_start',return_value='https://fixture.trycloudflare.com'),patch.object(C,'_ensure_server',return_value=None,create=True):
            self.assertEqual(C.cmd_connection(['public','--host'],None),0)
        state=json.loads(C.CONNECTION_STATE.read_text())
        self.assertEqual(state['transport_role'],'host')
        self.assertTrue(C._is_host())

    def test_revocation_reads_content_when_timestamp_is_preserved(self):
        conf_path=self.root/'agent.conf'
        conf_path.write_text(json.dumps(self.conf))
        with patch.object(S,'_LOADED_CONF',self.conf),patch.object(common,'CONF_PATH',conf_path),patch.object(S,'_CONFIG_STAMP',0):
            self.assertEqual(self.client.get('/api/health/').status_code,200)
            stamp=conf_path.stat()
            changed=dict(self.conf,vault_permissions={'Memory':{'alpha':'write','beta':'read'}})
            conf_path.write_text(json.dumps(changed))
            os.utime(conf_path,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
            with patch.object(S,'_next_task_number',return_value=7),patch.object(S,'_write_task'):
                result=self.post('/api/create/','beta',**{'from':'beta','to':'alpha','text':'revoked'})
            self.assertEqual(result.status_code,403,result.text)
    def test_host_with_url_becomes_consumer(self):
        self.conf['transport_role']='host'
        with patch.object(C,'_probe',return_value=True),patch.object(C,'_cloudflared_stop',return_value=True):
            self.assertEqual(C.cmd_connection(['public','https://peer.example'],None),0)
        self.assertFalse(C._is_host())
        self.assertEqual(C._endpoints()[0],'https://peer.example')

    def test_host_uses_own_queue_even_with_legacy_peer_lan_config(self):
        self.conf.update(transport_role='host',api_base='http://old-peer:8080')
        self.assertEqual(C._endpoints(),[C.LOCAL_HTTP])

    def test_public_consumer_never_falls_back_to_other_queue(self):
        C.CONNECTION_STATE.write_text(json.dumps({'mode':'public','transport_role':'consumer','public_url':'https://selected.example'}))
        self.assertEqual(C._endpoints(),['https://selected.example'])

    def test_inbox_epoch_survives_reads_and_changes_only_with_storage(self):
        headers={'X-Agent-Token':'alpha-token'}
        first=self.client.get('/api/inbox/alpha',headers=headers).json()['inbox_epoch']
        second=self.client.get('/api/inbox/alpha',headers=headers).json()['inbox_epoch']
        self.assertEqual(first,second)
        (self.root/'data/inbox-epoch').unlink()
        third=self.client.get('/api/inbox/alpha',headers=headers).json()['inbox_epoch']
        self.assertNotEqual(first,third)

    def test_stale_epoch_ack_cannot_ack_new_server_messages(self):
        headers={'X-Agent-Token':'alpha-token'}
        first=self.client.get('/api/inbox/alpha',headers=headers).json()['inbox_epoch']
        (self.root/'data/inbox-epoch').unlink()
        rejected=self.client.get('/api/inbox/alpha?since=66&ack=true&expected_epoch='+first,headers=headers)
        self.assertEqual(rejected.status_code,409)
        presence=json.loads((self.root/'data/alpha.presence.json').read_text())
        self.assertEqual(presence['acknowledged_through'],0)

    def test_read_agent_is_denied_canonical_preview_and_commit(self):
        for action,payload in [('preview',{'candidate':{}}),('commit',{'token':'not-a-token'})]:
            r=self.post('/api/vault/'+action,'alpha',**payload)
            self.assertEqual(r.status_code,403,r.text)

    def test_write_preview_tokens_are_bound_to_authenticated_actor(self):
        self.conf['vault_permissions']['Memory']['alpha']='write'
        with patch.object(S,'_writer_action',return_value={'ok':True,'token':'preview-fixture'},create=True):
            preview=self.post('/api/vault/preview','alpha',candidate={})
            self.assertEqual(preview.status_code,200,preview.text)
            committed=self.post('/api/vault/commit','beta',token=preview.json()['token'])
        self.assertEqual(committed.status_code,403,committed.text)

    def test_lost_commit_response_can_retry_without_duplicate_save(self):
        with patch.object(S,'_writer_action',side_effect=[{'ok':True,'token':'commit-fixture'}, {'operation':'create','sha256':'fixture-hash'}]) as action:
            preview=self.post('/api/vault/preview','beta',candidate={})
            token=preview.json()['token']
            first=self.post('/api/vault/commit','beta',token=token)
            retry=self.post('/api/vault/commit','beta',token=token)
        self.assertEqual(first.status_code,200,first.text)
        self.assertEqual(retry.status_code,200,retry.text)
        self.assertEqual(retry.json()['sha256'],'fixture-hash')
        self.assertTrue(retry.json()['duplicate'])
        self.assertEqual(action.call_count,2)
        denied=self.post('/api/vault/commit','alpha',token=token)
        self.assertEqual(denied.status_code,403)


if __name__=='__main__': unittest.main(verbosity=2)
