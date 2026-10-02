#!/usr/bin/env python3
import hashlib, importlib.util, os, tempfile, unittest
from pathlib import Path

WRITER_PATH = Path(os.environ.get('SKMR_WRITER_PATH', '/root/.claude/skills/skmr/scripts/obsidian_memory.py'))
spec = importlib.util.spec_from_file_location('writer_hardened', WRITER_PATH)
W = importlib.util.module_from_spec(spec); spec.loader.exec_module(W)

class ProfileHardening(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.vault = root/'vault'; self.state = root/'state'
        self.vault.mkdir(); self.state.mkdir()
        self.s = W.VaultStore(self.vault, self.state)
    def tearDown(self): self.tmp.cleanup()

    def cand(self, section, title, content='x', links=None, **extra):
        d={'semantic_class':'user-profile','profile_username':'anezatra','profile_section':section,
           'title':title,'status':'user-provided','provenance':'user-direct',
           'explicit_user_request':True,'automatic':False,'content':content,'links':links or []}
        d.update(extra); return d

    def commit(self, c):
        p=self.s.preview(c)
        if 'token' in p: return self.s.commit(p['token'])
        return p

    def test_profile_invariants_and_merge_proof(self):
        with self.assertRaises(W.PolicyDenied):
            self.s.preview(self.cand('index','Wrong title'))
        self.commit(self.cand('index','Anezatra — Profile',content='Canonical profile index.'))
        with self.assertRaises(W.PolicyDenied):
            self.s.preview(self.cand('identity','Anezatra — Identity',content='Identity.',links=['Unrelated']))
        self.commit(self.cand('identity','Anezatra — Identity',content='Identity durable fact.',links=['Anezatra — Profile']))
        check=self.s.validate_vault()
        self.assertIn(('05_Profiles/anezatra/00_Profile_Index.md'.replace("05_Profiles", W.PROFILES_DIR)), check['errors'])
        index=self.vault/('05_Profiles/anezatra/00_Profile_Index.md'.replace("05_Profiles", W.PROFILES_DIR))
        merged=self.cand('index','Anezatra — Profile',content='Canonical profile index.',links=['Anezatra — Identity'],
                         profile_full_merge=True,base_sha256=hashlib.sha256(index.read_bytes()).hexdigest())
        self.commit(merged)
        identity=self.vault/('05_Profiles/anezatra/01_Identity.md'.replace("05_Profiles", W.PROFILES_DIR))
        with self.assertRaises(W.PolicyDenied):
            self.s.preview(self.cand('identity','Anezatra — Identity',content='replacement',links=['Anezatra — Profile']))
        with self.assertRaises(W.PolicyDenied):
            self.s.preview(self.cand('identity','Anezatra — Identity',content='replacement',links=['Anezatra — Profile'],
                                     profile_full_merge=True,base_sha256='0'*64))
        valid=self.cand('identity','Anezatra — Identity',content='Identity durable fact.\n\nNew merged fact.',links=['Anezatra — Profile'],
                        profile_full_merge=True,base_sha256=hashlib.sha256(identity.read_bytes()).hexdigest())
        self.assertIn('token', self.s.preview(valid))
        self.assertTrue(self.s.validate_vault()['ok'])

    def test_profile_section_search(self):
        self.commit(self.cand('index','Anezatra — Profile',content='Index.'))
        self.commit(self.cand('devices-technical','Anezatra — Devices and Technical Environment',content='Laptop RAM 32 GB.',links=['Anezatra — Profile']))
        result=self.s.search('ram',profile='anezatra',profile_section='devices-technical')
        self.assertEqual(result['profile_section'],'devices-technical')
        self.assertTrue(result['matches'])
        with self.assertRaises(W.PolicyDenied): self.s.search('ram',profile_section='devices-technical')

if __name__=='__main__': unittest.main(verbosity=2)
