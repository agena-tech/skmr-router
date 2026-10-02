"""Real loopback HTTP integration for canonical writes and asyncRewake delivery."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import sys
import threading
import time
import unittest
from unittest.mock import patch
import uvicorn
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from test_symmetry import Symmetry
import server as S


class WireSymmetry(Symmetry):
    def setUp(self):
        super().setUp()
        self.sock=socket.socket(); self.sock.bind(('127.0.0.1',0))
        self.url='http://127.0.0.1:'+str(self.sock.getsockname()[1])
        self.host=uvicorn.Server(uvicorn.Config(S.app,log_level='critical',lifespan='off'))
        self.thread=threading.Thread(target=self.host.run,kwargs={'sockets':[self.sock]},daemon=True)
        self.thread.start()
        deadline=time.monotonic()+5
        while not self.host.started and time.monotonic()<deadline: time.sleep(.01)
        self.assertTrue(self.host.started)

    def tearDown(self):
        self.host.should_exit=True; self.thread.join(5); self.sock.close()
        super().tearDown()

    def test_real_wake_resume_stop_and_new_epoch_deliver_without_cursor_loss(self):
        path=Path(__file__).parent.parent/'.claude/bin/agent-message-watcher.py'
        spec=importlib.util.spec_from_file_location('wire_watcher',path)
        w=importlib.util.module_from_spec(spec);spec.loader.exec_module(w)
        w.DATA_DIR=self.root/'client-data';w.STATE_DIR=self.root/'state';w.PENDING_DIR=w.STATE_DIR/'agentcomm-pending'
        conf=dict(self.conf,api_base=self.url,server_local_url=self.url,api_lan='',token='alpha-token',transport_role='host')
        key='alpha-wire-session'
        self.post('/api/message/','beta',**{'from':'beta','to':'alpha','text':'first actual HTTP wake'})
        with contextlib.redirect_stderr(io.StringIO()) as out:
            self.assertEqual(w.run_watcher(conf,'alpha','beta',key,'sessionstart'),2)
        self.assertIn('first actual HTTP wake',out.getvalue())
        self.assertFalse(w.shared_cursor_path('alpha').exists())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(w.run_watcher(conf,'alpha','beta',key,'sessionstart'),2)
        self.post('/api/message/','beta',**{'from':'beta','to':'alpha','text':'second queued message'})
        with contextlib.redirect_stderr(io.StringIO()) as out:
            self.assertEqual(w.run_watcher(conf,'alpha','beta',key,'stop'),2)
        self.assertIn('second queued message',out.getvalue())
        self.assertEqual(w.shared_cursor_path('alpha').read_text(),'1')
        (self.root/'data/inbox-epoch').unlink()
        (self.root/'messages/alpha.jsonl').unlink()
        self.post('/api/message/','beta',**{'from':'beta','to':'alpha','text':'new host starts at one'})
        with contextlib.redirect_stderr(io.StringIO()) as out:
            self.assertEqual(w.run_watcher(conf,'alpha','beta',key,'sessionstart'),2)
        self.assertIn('new host starts at one',out.getvalue())
        archived=list((w.DATA_DIR/'epoch-archive').rglob('alpha-wire-session.json'))
        self.assertTrue(archived)
        self.assertIn('second queued message',archived[0].read_text())

    def test_actual_guarded_preview_commit_from_write_peer_with_read_host(self):
        writer=S._writer_module()
        import skmr_agent_policy as policy
        store=writer.VaultStore(self.vault,self.root/'fixtures')
        for title,link in [('Anchor','Index'),('Index','Anchor')]:
            item={'title':title,'semantic_class':'reference','content':'Fixture graph anchor.',
                  'links':[link],'automatic':False,'explicit_user_request':True,'status':'user-provided'}
            relative,kind,area=store.validate_candidate(item)
            target=self.vault/relative;target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text(store.render(item,kind,area))
        candidate={'title':'Independent writer','semantic_class':'reference',
                   'content':'Preserve the guarded preview before committing a permanent note.',
                   'links':['Anchor'],'automatic':False,'explicit_user_request':True,'status':'user-provided'}
        # State the simulated condition instead of inheriting it from whatever
        # this machine's agent.conf happens to say: is_mount=True only means
        # anything under the 'smb' backing, so the backing is pinned here too.
        with patch.object(policy,'VAULT',self.vault), \
             patch.object(policy,'vault_backing',lambda:policy.SMB_BACKING), \
             patch.object(Path,'is_mount',return_value=True):
            r=self.post('/api/vault/preview','beta',candidate=candidate)
            self.assertEqual(r.status_code,200,r.text)
            self.assertIn('diff',r.json())
            commit=self.post('/api/vault/commit','beta',token=r.json()['token'])
            self.assertEqual(commit.status_code,200,commit.text)
            self.assertEqual(commit.json()['operation'],'create')
        self.assertIn('Preserve the guarded',Path(commit.json()['target']).read_text())
        self.assertFalse((self.root/'data/writer-state/skmr-obsidian-transactions').exists() and
                         list((self.root/'data/writer-state/skmr-obsidian-transactions').iterdir()))

    def test_authenticated_storage_redirect_never_delivers_token_to_second_origin(self):
        received=[]
        class Sink(BaseHTTPRequestHandler):
            def do_GET(inner):
                received.append(inner.headers.get('X-Agent-Token'))
                inner.send_response(200);inner.end_headers();inner.wfile.write(b'{"ok":true}')
            def log_message(*args): pass
        sink=ThreadingHTTPServer(('127.0.0.1',0),Sink)
        class Redirect(BaseHTTPRequestHandler):
            def do_GET(inner):
                inner.send_response(302)
                inner.send_header('Location','http://localhost:'+str(sink.server_port)+'/capture')
                inner.end_headers()
            def log_message(*args): pass
        origin=ThreadingHTTPServer(('127.0.0.1',0),Redirect)
        threads=[threading.Thread(target=server.serve_forever,daemon=True) for server in (sink,origin)]
        for thread in threads:thread.start()
        self.conf['vault_origin']='http://127.0.0.1:'+str(origin.server_port)
        try:
            with self.assertRaises(S.HTTPException) as error:
                S._storage_proxy('/api/vault/list','beta')
            self.assertEqual(error.exception.status_code,502)
            self.assertEqual(received,[])
        finally:
            for server in (origin,sink):server.shutdown();server.server_close()
            for thread in threads:thread.join(3)


if __name__=='__main__':unittest.main(verbosity=2)
