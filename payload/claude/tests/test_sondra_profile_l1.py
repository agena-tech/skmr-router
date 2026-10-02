from __future__ import annotations
import importlib.util, tempfile, unittest
from pathlib import Path
WRITER = "/root/.claude/skills/skmr/scripts/obsidian_memory.py"
spec = importlib.util.spec_from_file_location("skmr_writer_test", WRITER)
W = importlib.util.module_from_spec(spec); spec.loader.exec_module(W)

class ProfileCoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name); (self.root / W.PROFILES_DIR).mkdir()
        self.store = W.VaultStore(self.root, self.root / ".state")
    def tearDown(self): self.tmp.cleanup()
    def candidate(self, user, section, title, content="durable fact", links=None):
        return {"semantic_class":"user-profile","profile_username":user,"profile_section":section,"title":title,"status":"user-provided","provenance":"user-direct","automatic":True,"explicit_user_request":False,"novel":True,"reusable":True,"verified":False,"content":content,"links":[] if links is None else links}
    def write(self, c):
        rel, typ, area = self.store.validate_candidate(c); path = self.root / rel; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(self.store.render(c, typ, area), encoding="utf-8"); return path
    def test_routes_all_profile_sections(self):
        for section, filename in W.PROFILE_SECTIONS.items():
            c = {"semantic_class":"user-profile","profile_username":"anezatra","profile_section":section,
                 "title":W.expected_profile_title("anezatra", section)}
            rel, typ, area = self.store.route(c)
            self.assertEqual(rel, Path(W.PROFILES_DIR) / "anezatra" / filename)
            self.assertEqual((typ, area), ("user-profile","profile"))

    def test_profile_title_must_be_canonical(self):
        """route_profile owns the canonical title; a candidate cannot rename a section."""
        c = {"semantic_class":"user-profile","profile_username":"anezatra",
             "profile_section":"identity","title":"Benim Uydurdugum Baslik"}
        with self.assertRaises(W.PolicyDenied): self.store.route(c)

    def test_unknown_profile_section_rejected(self):
        c = {"semantic_class":"user-profile","profile_username":"anezatra",
             "profile_section":"boyle-bir-bolum-yok","title":"x"}
        with self.assertRaises(W.PolicyDenied): self.store.route(c)
    def test_unsafe_username(self):
        with self.assertRaises(W.PolicyDenied): self.store.route({"semantic_class":"user-profile","profile_username":"../escape","profile_section":"identity"})
    def test_generic_stems_do_not_collide(self):
        aidx = self.candidate("anezatra","index","Anezatra — Profile"); self.write(aidx)
        bidx = self.candidate("berke","index","Berke — Profile"); self.write(bidx)
        aid = self.candidate("anezatra","identity","Anezatra — Identity",links=["Anezatra — Profile"]); self.write(aid)
        bid = self.candidate("berke","identity","Berke — Identity",links=["Berke — Profile"]); self.write(bid)
        aidx["links"]=["Anezatra — Identity"]; (self.root/W.PROFILES_DIR/"anezatra"/"00_Profile_Index.md").write_text(self.store.render(aidx,"user-profile","profile"),encoding="utf-8")
        bidx["links"]=["Berke — Identity"]; (self.root/W.PROFILES_DIR/"berke"/"00_Profile_Index.md").write_text(self.store.render(bidx,"user-profile","profile"),encoding="utf-8")
        result = self.store.validate_vault(); self.assertEqual(result["duplicates"], {}); self.assertEqual(result["broken_links"], {})
    def test_profile_search_is_scoped(self):
        for user, name in (("anezatra","Anezatra"),("berke","Berke")):
            idx=self.candidate(user,"index",f"{name} — Profile"); self.write(idx)
            ident=self.candidate(user,"identity",f"{name} — Identity",content=f"{name} laptop ram",links=[f"{name} — Profile"]); self.write(ident)
        result=self.store.search("laptop ram", profile="anezatra")
        self.assertTrue(result["matches"])
        self.assertEqual(result["profile"], "anezatra")
        prefix = f"{W.PROFILES_DIR}/anezatra/"
        self.assertTrue(all(m["path"].startswith(prefix) for m in result["matches"]),
                        [m["path"] for m in result["matches"]])
        # Kapsamın gerçekten filtre olduğunu göster: kapsamsız arama diğer profili de görür.
        unscoped = self.store.search("laptop ram")
        self.assertTrue(any(m["path"].startswith(f"{W.PROFILES_DIR}/berke/") for m in unscoped["matches"]),
                        [m["path"] for m in unscoped["matches"]])
    def test_user_provided_profile_needs_no_technical_evidence(self):
        c=self.candidate("anezatra","index","Anezatra — Profile"); self.store.validate_candidate(c)
    def test_verified_profile_still_needs_evidence(self):
        c=self.candidate("anezatra","index","Anezatra — Profile"); c["status"]="verified"; c["verified"]=True
        with self.assertRaises(W.PolicyDenied): self.store.validate_candidate(c)

if __name__ == "__main__": unittest.main()
