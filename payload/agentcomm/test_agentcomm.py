import concurrent.futures, importlib.util, io, json, os, sys, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
sys.path.insert(0, str(Path(__file__).parent))
import server as S
import send as C

class Communication(unittest.TestCase):
    """Rules under test, not the roster.

    WRITER/READER name the two grants rather than two agents, so the swapped
    subclass below re-runs every case with the assignment reversed. A test that
    silently depends on a particular agent name fails there instead of passing
    forever on the machine it was written on.
    """
    WRITER = 'eliza'
    READER = 'sondra'

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.msg=self.root/'messages'; self.msg.mkdir()
        self.data=self.root/'data'; self.data.mkdir()
        self.writer,self.reader=self.WRITER,self.READER
        self.conf={'self':self.writer,'peer':self.reader,
                   'tokens':{self.writer:self.writer+'-test',self.reader:self.reader+'-test'},
                   'fail_seconds':300,'passive_seconds':600,'vault_name':'Memory',
                   'vault_permissions':{'Memory':{self.writer:'write',self.reader:'read'}}}
        self.patches=[patch.object(S,'MSG_DIR',self.msg),patch.object(S,'DATA_DIR',self.data),patch.object(S,'CONF',self.conf),
                      patch.object(C,'_connection_state',return_value={'mode':'private','transport_role':'consumer'})]
        for p in self.patches:p.start()
        self.client=TestClient(S.app)
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def post(self,path,body,actor=None):
        actor=actor or self.writer
        return self.client.post(path,json=body,headers={'X-Agent-Token':actor+'-test'})
    def get(self,path,actor=None):
        actor=actor or self.writer
        return self.client.get(path,headers={'X-Agent-Token':actor+'-test'})
    def message(self,**overrides):
        return dict({'from':self.writer,'to':self.reader,'text':'audit fixture','request_id':'fixture-1234'},**overrides)
    def test_bidirectional_mail_and_own_inbox(self):
        for actor,peer in [(self.writer,self.reader),(self.reader,self.writer)]:
            self.assertEqual(self.post('/api/message/',{'from':actor,'to':peer,'text':'hello'},actor).status_code,200)
            self.assertEqual(len(self.get('/api/inbox/'+peer,peer).json()['messages']),1)
        self.assertEqual(self.get('/api/inbox/'+self.reader).status_code,403)
    def test_no_auth_and_spoofed_identity(self):
        self.assertEqual(self.client.post('/api/message/',json=self.message()).status_code,401)
        self.assertEqual(self.post('/api/message/',self.message(),self.reader).status_code,403)
        self.assertEqual(self.get('/api/status/','unknown').status_code,401)
    def test_path_traversal_and_bounds(self):
        for to in ('../escape','/tmp/escape'):
            self.assertEqual(self.post('/api/message/',self.message(to=to)).status_code,422)
        self.assertEqual(self.post('/api/message/',self.message(to='unknown')).status_code,403)
        self.assertEqual(self.post('/api/message/',self.message(text='')).status_code,422)
        self.assertEqual(self.post('/api/message/',self.message(text='a'*65537)).status_code,422)
        self.assertEqual(self.get('/api/inbox/'+self.writer+'?since=-1').status_code,422)
    def test_message_retry_idempotent_and_conflicts(self):
        first=self.post('/api/message/',self.message()).json()
        second=self.post('/api/message/',self.message()).json()
        self.assertEqual(first['id'],second['id'])
        self.assertEqual(self.post('/api/message/',self.message(text='changed')).status_code,409)
        self.assertEqual(len(S._read_after(self.reader,0)),1)
    def test_concurrent_message_sequence(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            ids=list(pool.map(lambda i:self.post('/api/message/',self.message(request_id='fixture-'+str(i))).json()['id'],range(24)))
        self.assertEqual(sorted(ids),list(range(1,25)))
    def test_concurrent_task_numbering_and_retry(self):
        tasks={}
        def allocate():return max(tasks,default=0)+1
        def write(n,*args):
            time.sleep(.002)
            self.assertNotIn(n,tasks); tasks[n]=args
        with patch.object(S,'_next_task_number',side_effect=allocate),patch.object(S,'_write_task',side_effect=write):
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                numbers=list(pool.map(lambda i:self.post('/api/create/',self.message(request_id='tasktest-'+str(i))).json()['task_no'],range(16)))
            self.assertEqual(sorted(numbers),list(range(1,17)))
            self.assertEqual(self.post('/api/create/',self.message(request_id='tasktest-0')).json()['task_no'],numbers[0])
            self.assertEqual(len(tasks),16)
    def test_read_grant_cannot_create_tasks(self):
        with patch.object(S,'_write_task') as write:
            self.assertEqual(self.post('/api/create/',{'from':self.reader,'to':self.writer,'text':'no'},self.reader).status_code,403)
            write.assert_not_called()
    def test_peer_commander_task_uses_its_own_storage_and_shared_inbox(self):
        self.conf.update(peer_vault_name='PeerMemory', peer_api_lan='http://peer.example:8080')
        self.conf['vault_permissions']['PeerMemory']={self.writer:'read',self.reader:'write'}
        task={'from':self.reader,'to':self.writer,'text':'own vault task','request_id':'peer-task-1234'}
        with patch.object(S,'_storage_proxy',return_value={'ok':True,'task_no':42}) as proxy, \
             patch.object(S,'_write_task') as local_write:
            result=self.post('/api/create/',task,self.reader)
            self.assertEqual(result.status_code,200,result.text)
            self.assertEqual(result.json()['task_no'],42)
            self.assertEqual(proxy.call_args.kwargs['origin'],'http://peer.example:8080')
            local_write.assert_not_called()
        messages=S._read_after(self.writer,0)
        self.assertEqual(messages[0]['type'],'task')
        self.assertEqual(messages[0]['from'],self.reader)
        self.assertEqual(self.conf['vault_permissions']['Memory'][self.reader],'read')
    def test_peer_task_route_requires_write_access_to_its_own_vault(self):
        self.conf.update(peer_vault_name='PeerMemory', peer_api_lan='http://peer.example:8080')
        self.conf['vault_permissions']['PeerMemory']={self.writer:'write',self.reader:'read'}
        with patch.object(S,'_storage_proxy') as proxy:
            result=self.post('/api/create/',{'from':self.reader,'to':self.writer,'text':'denied'},self.reader)
            self.assertEqual(result.status_code,403)
            proxy.assert_not_called()
    def test_smb_list_failure_never_allocates(self):
        import subprocess
        result=subprocess.CompletedProcess([],1,stdout='',stderr='fixture')
        with patch.object(S,'_smb_base',return_value=['smbclient']),patch.object(S.subprocess,'run',return_value=result),patch.object(S,'_write_task') as write:
            self.assertEqual(self.post('/api/create/',self.message()).status_code,502)
            write.assert_not_called()
        self.assertFalse((self.data/'task_counter.json').exists())
    def test_deleted_task_numbers_are_not_reused(self):
        import subprocess
        (self.data/'task_counter.json').write_text(json.dumps({'n':40}))
        response=subprocess.CompletedProcess([],0,stdout='  TASK-3.md A 0 today\n',stderr='')
        with patch.object(S,'_smb_base',return_value=['smbclient']),patch.object(S.subprocess,'run',return_value=response):
            self.assertEqual(S._next_task_number(),41)
            self.assertEqual(S._next_task_number(),42)
    def test_presence_thresholds(self):
        self.assertEqual(self.get('/api/status/').json()['state'],'unknown')
        for seconds,state in [(10,'recently_seen'),(301,'fallback_due'),(601,'passive')]:
            (self.data/(self.reader+'.presence.json')).write_text(json.dumps({'seen':time.time()-seconds}))
            self.assertEqual(self.get('/api/status/').json()['state'],state)
    def test_read_presence_is_not_assumed_from_queued_message(self):
        self.post('/api/message/',self.message())
        self.assertEqual(self.get('/api/status/').json()['state'],'unknown')
        self.get('/api/inbox/'+self.reader+'?since=1&ack=true',self.reader)
        self.assertEqual(self.get('/api/status/').json()['acknowledged_through'],1)
    def test_poll_minimum_and_auth_errors_not_retried(self):
        import urllib.error
        self.assertEqual(C.cmd_watch(0),3)
        with patch.object(C,'http_open',side_effect=urllib.error.HTTPError('fixture',403,'denied',{},None)) as request:
            with self.assertRaisesRegex(RuntimeError,'403'):C._request('GET','/api/inbox/'+self.writer)
            self.assertEqual(request.call_count,1)
    def test_fallback_delayed_and_https_only(self):
        import urllib.error
        conf={'api_base':'http://127.0.0.1:1','api_lan':'http://127.0.0.1:1','api_fallback':'https://fixture.invalid','token':'fixture','fail_seconds':300}
        (self.root/'data'/(self.writer+'.outage')).write_text(str(time.time()-100))
        with patch.object(C,'CONF',conf),patch.object(C.common,'BASE_DIR',self.root),patch.object(C,'SELF',self.writer),patch.object(C,'http_open',side_effect=urllib.error.URLError('fixture')) as request:
            with self.assertRaises(RuntimeError):C._request('GET','/api/health/')
            self.assertEqual(request.call_count,1)
            (self.root/'data'/(self.writer+'.outage')).write_text(str(time.time()-301))
            request.reset_mock()
            with self.assertRaises(RuntimeError):C._request('GET','/api/health/')
            self.assertEqual(request.call_count,2)
            conf['api_fallback']='http://fixture.invalid'
            with self.assertRaisesRegex(RuntimeError,'HTTPS'):C._request('GET','/api/health/')
    def test_color_label_is_trusted_and_local_when_enabled(self):
        # When color is enabled, ONLY the label is colored, with a locally
        # generated code. The code is asserted through agent_color() rather than
        # a literal, so an agent named anything at all is covered.
        clean={k:v for k,v in os.environ.items() if k not in ('NO_COLOR','AGENTCOMM_NO_COLOR')}
        with patch.dict(os.environ,{**clean,'AGENTCOMM_FORCE_COLOR':'1'},clear=True):
            for who in (self.reader,self.writer,'Gamma'):
                line=C.common.format_incoming(who,'body')
                expected='\x1b['+C.common.agent_color(who)+'m'+who.upper()+':\x1b[0m '
                self.assertTrue(line.startswith(expected),line)
                self.assertTrue(line.endswith(' body'))
                self.assertEqual(line.count('\x1b'),2)  # exactly the label's open+reset
            self.assertEqual(C.common.format_task_created(8),'Task 8 Created.')  # never colored

    def test_peer_escape_cannot_inject_terminal_formatting(self):
        # Peer-supplied escape/control bytes must never reach the terminal, in
        # either color mode. With color on, the only escapes are the trusted label.
        evil='\x1b[31mHACK\x07\x08 tail'
        clean={k:v for k,v in os.environ.items() if k not in ('NO_COLOR','AGENTCOMM_NO_COLOR','AGENTCOMM_FORCE_COLOR')}
        with patch.dict(os.environ,{**clean,'AGENTCOMM_NO_COLOR':'1'},clear=True):
            plain=C.common.format_incoming(self.reader,evil)
            self.assertNotIn('\x1b',plain); self.assertNotIn('\x07',plain)
            self.assertIn('HACK tail',plain)
            self.assertNotIn('\x1b',C.common.format_feed({'from':self.reader,'text':evil,'id':1}))
        with patch.dict(os.environ,{**clean,'AGENTCOMM_FORCE_COLOR':'1'},clear=True):
            colored=C.common.format_incoming(self.reader,evil)
            self.assertEqual(colored.count('\x1b'),2)  # label only; body stripped

    def test_plain_mode_available_and_default_off_tty(self):
        clean={k:v for k,v in os.environ.items() if k not in ('NO_COLOR','AGENTCOMM_NO_COLOR','AGENTCOMM_FORCE_COLOR')}
        # Explicit opt-out wins even if forced.
        with patch.dict(os.environ,{**clean,'AGENTCOMM_NO_COLOR':'1','AGENTCOMM_FORCE_COLOR':'1'},clear=True):
            self.assertEqual(C.common.format_incoming(self.reader,'body'),self.reader.upper()+': body')
        # With neither override set, color follows the target stream's TTY-ness;
        # a non-TTY stream (a pipe/file) stays plain.
        with patch.dict(os.environ,clean,clear=True):
            self.assertFalse(C.common.color_enabled(io.StringIO()))
            with patch('sys.stdout',io.StringIO()):
                self.assertEqual(C.common.format_incoming(self.reader,'body'),self.reader.upper()+': body')

class CommunicationWithGrantsSwapped(Communication):
    """Every case above, with the write grant on the other agent.

    This is the guard against a suite that proves only today's roster: if any
    assertion depends on an agent's name rather than on its grant, it fails here
    while passing in the class above.
    """
    WRITER = 'sondra'
    READER = 'eliza'


if __name__=='__main__':unittest.main(verbosity=2)
