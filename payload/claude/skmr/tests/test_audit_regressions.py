"""Independent SKMR audit: all state mutations use temporary stores."""
import contextlib, io, json, os, pathlib, sqlite3, sys, tempfile, unittest
from unittest.mock import patch
sys.path.insert(0, '/root/.claude')
from skmr.core import config, dispatcher, registry
from skmr.memory import native, intent
from skmr.agents import orchestrator as orc, topology
from skmr.planning import planner
from skmr.memory.retrieval import hybrid, vector, embeddings, tokenizer
from skmr.memory.indexing import index
from skmr.commands import obsidian_memory_cmd as memcmd, bugskills_cmd, send_cmd, doctor_cmd

class Audit(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=pathlib.Path(self.tmp.name)
        self.old=config._cache; self.old_provider=embeddings._provider
        config._cache=dict(config.DEFAULTS, state_dir=str(self.root/'state'),
            memory_path=str(self.root/'MEMORY.md'), agents_path=str(self.root/'agents.json'),
            claude_md=str(self.root/'CLAUDE.md'), index_path=str(self.root/'index.sqlite3'),
            vault_path=str(self.root/'vault'), color='never')
        (self.root/'vault').mkdir(); (self.root/'vault/a.md').write_text('# Alpha\n\nalpha sample knowledge\n')
        embeddings._provider=None
    def tearDown(self):
        config._cache=self.old; embeddings._provider=self.old_provider; self.tmp.cleanup()
    def plan(self, task='alpha work, beta work, gamma work'):
        return planner.build(registry.Skill('audit','Audit','x:y',planning='medium',subagents=True),task)
    def test_slash_namespace_resolves(self):
        self.assertEqual(registry.resolve('/skmr:help').name, 'help')
    def test_plan_has_one_planning_banner(self):
        out=io.StringIO()
        with contextlib.redirect_stdout(out): code=dispatcher.dispatch(['plan','audit auth, review code, check tests'])
        self.assertEqual(code,0); self.assertEqual(out.getvalue().count('Planning:'),1)
    def test_trivial_freeform_plan_stays_light(self):
        out=io.StringIO()
        with contextlib.redirect_stdout(out):dispatcher.dispatch(['plan','rename one variable'])
        self.assertNotIn('Plan (',out.getvalue());self.assertIn('No subagents required.',out.getvalue())
    def test_proposals_never_claim_spawn(self):
        out=io.StringIO()
        with contextlib.redirect_stdout(out): dispatcher.dispatch(['plan','audit auth, review code, check tests'])
        self.assertNotIn('subagents were called',out.getvalue())
    def test_dependent_prose_stays_serial(self):
        self.assertFalse(self.plan('audit endpoints, then analyze auth, then verify findings').subagents)
    def test_planning_failure_is_controlled(self):
        with patch('skmr.hooks.planning.run',side_effect=ValueError('invalid plan')):
            with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(dispatcher.dispatch(['help']),1)
    def test_unknown_result_rejected(self):
        with self.assertRaises(orc.TransitionError): orc.record(orc.Result('ghost','work','COMPLETED'))
    def test_result_cannot_skip_dependency(self):
        orc.register(self.plan())
        with self.assertRaises(orc.TransitionError): orc.record(orc.Result('verifier-agent','verify','COMPLETED'))
    def test_repeated_running_updates_progress(self):
        orc.register(self.plan()); orc.transition('unit-01-agent','RUNNING','first')
        orc.transition('unit-01-agent','RUNNING','milestone')
        self.assertEqual(native.load().subagents()[0].progress,'milestone')
    def test_registration_validates_duplicate_ids(self):
        plan=self.plan(); plan.subagents[1].agent_id=plan.subagents[0].agent_id
        with self.assertRaises(orc.TransitionError): orc.register(plan)
    def test_registration_preserves_active_work(self):
        orc.register(self.plan()); orc.transition('unit-01-agent','RUNNING','active')
        with self.assertRaises(orc.TransitionError): orc.register(self.plan('audit new A, new B, new C'))
        self.assertEqual(native.load().subagents()[0].status,'RUNNING')
    def test_details_are_in_memory(self):
        orc.register(self.plan())
        body=native.memory_path().read_text()
        self.assertIn('Depends on',body); self.assertIn('Expected Output',body)
    def test_table_escaping_round_trip(self):
        agent=native.Subagent('x','a | b\nnew line','RUNNING','p | q')
        native.mutate(lambda s:s.set_subagents([agent]))
        row=native.load().subagents()[0]
        self.assertEqual(row.task,'a | b new line'); self.assertEqual(row.status,'RUNNING'); self.assertEqual(row.progress,'p | q')
    def test_short_state_intents_work_offline(self):
        for phrase,want in [('continue','continue_task'),('hangi aşamadayız?','current_state'),
                ('what were we doing?','current_state')]:
            with self.subTest(phrase=phrase):
                result=intent.detect(phrase,allow_semantic=False); self.assertIsNotNone(result)
                self.assertEqual(result.intent,want)
    def test_peer_role_question_resolves_from_the_topology(self):
        # No agent name is hardcoded any more, so this depends on the persisted
        # topology rather than on the peer happening to be called Sondra.
        from skmr.agents import topology
        installed=topology.Topology(
            local=topology.Agent('Alpha','Commander',kind='local',vault_access='write'),
            remote=[topology.Agent('Sondra','Lieutenant',kind='remote',host='h',
                                   reports_to='Alpha',vault_access='read')])
        with patch.object(topology,'load',return_value=installed):
            result=intent.detect("Sondra’nın rolü ne?",allow_semantic=False)
        self.assertIsNotNone(result); self.assertEqual(result.intent,'identity_peer')
    def test_possessive_identity_and_next_step_route_offline(self):
        # CLAUDE.md names these phrasings directly; they must not need a question
        # mark or the semantic fallback to route.
        for phrase,want in [('benim adım ne','identity_user'),('what is my name','identity_user'),
                ('ismim ne','identity_user'),('sıradaki adım ne','next_step'),
                ('what is the next step','next_step'),('sonraki adım nedir','next_step')]:
            with self.subTest(phrase=phrase):
                result=intent.detect(phrase,allow_semantic=False)
                self.assertIsNotNone(result,f'no routing for {phrase!r}')
                self.assertEqual(result.intent,want)
                self.assertEqual(result.route,'native-memory')
    def test_next_step_does_not_swallow_possessive_name(self):
        self.assertEqual(intent.detect('benim adım ne',allow_semantic=False).intent,'identity_user')
        self.assertEqual(intent.detect('adın ne',allow_semantic=False).intent,'identity_self')
    def test_action_request_not_identity(self):
        self.assertIsNone(intent.detect('how do I change your name in settings?',allow_semantic=False))
    def test_unicode_lexical_tokens(self):
        self.assertIn('guclu',tokenizer.tokenize('güçlü')); self.assertIn('机器人',tokenizer.tokenize('机器人'))
    def test_vector_runtime_failure_falls_back(self):
        index.sync(embed=False)
        with patch.object(vector,'status',return_value=vector.VectorStatus(True)),patch.object(vector,'search',side_effect=RuntimeError('embed failed')):
            result=hybrid.search('alpha')
        self.assertTrue(result.ok); self.assertTrue(result.results)
        self.assertTrue(any('BM25' in w for w in result.warnings))
    def test_bm25_failure_uses_vector(self):
        index.sync(embed=False)
        hit=hybrid.Hit('fake','a.md','Alpha','alpha',0.8)
        with patch.object(hybrid.bm25,'search',side_effect=RuntimeError('postings unavailable')),patch.object(vector,'status',return_value=vector.VectorStatus(True)),patch.object(vector,'search',return_value=[hit]):
            result=hybrid.search('alpha')
        self.assertTrue(result.ok); self.assertEqual(result.results[0].source,'vector')
    def test_stale_same_width_vector_excluded(self):
        index.sync(embed=False); conn=index.connect()
        conn.execute('UPDATE chunks SET embedding=?,embed_sig=?',(index.pack([1,0]),'ollama:other:v1')); conn.commit()
        fake=unittest.mock.Mock(); fake.available.return_value=True; fake.embed.return_value=[[1,0]]; fake.signature.return_value='ollama:bge-m3:v1'
        try:
            with patch.object(vector,'provider',return_value=fake): self.assertEqual(vector.search(conn,'alpha'),[])
        finally: conn.close()
    def test_healthy_empty_search_not_error(self):
        index.sync(embed=False)
        with patch.object(vector,'status',return_value=vector.VectorStatus(False,'offline')):
            result=hybrid.search('zzzzunmatched')
        self.assertTrue(result.ok); self.assertEqual(result.results,[])
    def test_profile_options_preserved(self):
        with patch.object(memcmd,'_delegate',return_value=0) as delegate:
            self.assertEqual(memcmd._search(['my laptop','--profile','anezatra','--profile-section','devices-technical','--limit','2']),0)
            self.assertEqual(delegate.call_args.args[0],['search','my laptop','--profile','anezatra','--profile-section','devices-technical','--limit','2'])
    def test_negative_search_limit_rejected(self):
        self.assertEqual(memcmd._search(['alpha','--limit','-1']),2)
    def test_missing_flag_value_rejected(self):
        self.assertEqual(memcmd._search(['alpha','--mode']),2)
    def test_bad_grep_pattern_controlled(self):
        with patch.object(bugskills_cmd,'ROOT',self.root/'vault'):
            out=io.StringIO()
            with contextlib.redirect_stdout(out): code=bugskills_cmd._search(['['])
            self.assertEqual(code,0); self.assertIn('No BugBountySkills match',out.getvalue())
    def test_unknown_send_peer_never_delivered(self):
        topology.save(topology.Topology(local=topology.Agent('Eliza','Commander',vault_access='write'),remote=[topology.Agent('Sondra','Lieutenant','localhost','Eliza','remote')]))
        with patch.object(send_cmd,'_run') as run:
            self.assertEqual(send_cmd.run(['unrelated-peer','message'],self.plan()),2); run.assert_not_called()
    def _send_conf(self):
        """A synthetic agent.conf so the dispatch does not read the live one."""
        conf = self.root / 'agent.conf'
        conf.write_text(json.dumps({'self': 'eliza', 'peer': 'sondra'}), encoding='utf-8')
        topology.save(topology.Topology(
            local=topology.Agent('Eliza', 'Commander', vault_access='write'),
            remote=[topology.Agent('Sondra', 'Lieutenant', 'localhost', 'Eliza', 'remote')]))
        return patch.dict(os.environ, {'AGENTCOMM_CONF': str(conf)})

    def test_a_single_message_needs_no_peer_name(self):
        # `/skmr:send "text"` was rejected as an unknown action, which made the
        # obvious form unusable while exactly one peer is configured.
        with self._send_conf(), patch.object(send_cmd, '_run') as run:
            run.return_value = 0
            self.assertEqual(send_cmd.run(['Selam Eliza. Ben Endra.'], self.plan()), 0)
            run.assert_called_once_with(['message', 'Selam Eliza. Ben Endra.'])

    def test_a_bare_peer_name_asks_for_the_text(self):
        with self._send_conf(), patch.object(send_cmd, '_run') as run:
            self.assertEqual(send_cmd.run(['sondra'], self.plan()), 2)
            run.assert_not_called()

    def test_a_mistyped_subcommand_is_not_sent_as_a_message(self):
        # `heath` must be reported as a probable typo for `health`, not queued.
        with self._send_conf(), patch.object(send_cmd, '_run') as run:
            self.assertEqual(send_cmd.run(['heath'], self.plan()), 2)
            run.assert_not_called()

    def test_peer_plus_text_still_works(self):
        with self._send_conf(), patch.object(send_cmd, '_run') as run:
            run.return_value = 0
            self.assertEqual(send_cmd.run(['sondra', 'iki', 'argumanli'], self.plan()), 0)
            run.assert_called_once_with(['message', 'iki argumanli'])

    def test_known_subcommands_are_passed_through_unchanged(self):
        with self._send_conf(), patch.object(send_cmd, '_run') as run:
            run.return_value = 0
            self.assertEqual(send_cmd.run(['health'], self.plan()), 0)
            run.assert_called_once_with(['health'])

    def test_role_control_char_rejected(self):
        with self.assertRaises(topology.ValidationError): topology.validate_role('Commander\n## injected')
    def test_invalid_hostname_label_length_rejected(self):
        with self.assertRaises(topology.ValidationError): topology.validate_host('a'*64+'.example')
    def test_doctor_inspects_validator_ok(self):
        completed=unittest.mock.Mock(returncode=0,stdout='{"ok":false,"errors":{"bad.md":["invalid"]}}',stderr='')
        with patch.object(doctor_cmd.subprocess,'run',return_value=completed):
            status,detail=doctor_cmd._writer()
        self.assertNotEqual(status,doctor_cmd.OK)
    def test_runtime_hook_is_installed(self):
        settings=json.loads(pathlib.Path('/root/.claude/settings.json').read_text())
        for event in ['UserPromptSubmit','SubagentStart','SubagentStop','PostToolUse']:
            hooks=[h for entry in settings['hooks'].get(event,[]) for h in entry['hooks']]
            self.assertTrue(any('skmr-runtime.py' in ' '.join([h['command'],*h.get('args',[])]) for h in hooks),event)
    def test_semantic_provider_construction_failure_is_safe(self):
        with patch.object(embeddings,'provider',side_effect=ValueError('bad provider')):
            self.assertIsNone(intent.detect('unrelated phrasing not in cues'))
    def test_runtime_reads_actual_state(self):
        from skmr.hooks import runtime
        native.mutate(lambda s:s.set('Next','resume audit fixture'))
        result=runtime.run_event({'hook_event_name':'UserPromptSubmit','prompt':'nerede kalmıştık?'})
        self.assertIn('resume audit fixture',result['hookSpecificOutput']['additionalContext'])
    def test_actual_agent_lifecycle_and_blocker(self):
        from skmr.hooks import runtime
        start={'hook_event_name':'SubagentStart','session_id':'test','agent_id':'abc','agent_type':'tester'}
        runtime.run_event(start); rows=native.load().subagents(); self.assertEqual(rows[0].status,'RUNNING')
        runtime.run_event(dict(start,hook_event_name='SubagentStop',last_assistant_message=json.dumps({
            'status':'BLOCKED','summary':'fixture blocked','open_problems':['missing fixture input'],'next_step':'provide fixture input'})))
        self.assertEqual(native.load().subagents()[0].status,'BLOCKED')
        self.assertIn('missing fixture input',native.load().get('Open Problems'))
    def test_runtime_skill_load_not_task_completion(self):
        from skmr.hooks import runtime
        event={'hook_event_name':'PreToolUse','tool_name':'Skill','tool_input':{'skill':'skmr','args':'search knowledge'}}
        runtime.run_event(event); runtime.run_event(dict(event,hook_event_name='PostToolUse'))
        self.assertIn('verification pending',native.load().get('Phase'))
    def test_concurrent_results_not_lost(self):
        import multiprocessing
        orc.register(self.plan())
        ids=['unit-01-agent','unit-02-agent','unit-03-agent']
        for key in ids:orc.transition(key,'RUNNING')
        def finish(key):orc.record(orc.Result(key,'work','COMPLETED',summary=key,findings=[key]))
        workers=[multiprocessing.get_context('fork').Process(target=finish,args=(key,)) for key in ids]
        for worker in workers:worker.start()
        for worker in workers:worker.join(5); self.assertEqual(worker.exitcode,0)
        self.assertEqual(len(orc.aggregate()['findings']),3)
    def test_partial_remote_flags_never_clear_topology(self):
        from skmr.commands import assign_role_cmd
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(assign_role_cmd.run(['--non-interactive','--name','A','--role','R','--remote-name',''],self.plan()),2)
        self.assertFalse(topology.path().exists())
    def test_provider_tracks_model_changes(self):
        first=embeddings.provider(); config._cache['embedding_model']='another-model'
        self.assertEqual(embeddings.provider().model,'another-model')
    def test_chunk_size_change_invalidates(self):
        index.sync(embed=False); config._cache['chunk_max_chars']=100
        self.assertTrue(index.sync(embed=False).invalidated)
    def test_embedding_short_response_never_reports_complete(self):
        index.sync(embed=False)
        fake=unittest.mock.Mock();fake.available.return_value=True;fake.embed.return_value=[]
        conn=index.connect()
        try:
            with patch.object(embeddings,'provider',return_value=fake):
                count,reason=index._embed_pending(conn,'fake:v1')
            self.assertEqual(count,0);self.assertTrue(reason)
        finally:conn.close()
    def test_role_update_failure_rolls_back_topology(self):
        from skmr.commands import assign_role_cmd
        conf_path=self.root/'agent.conf'
        conf_path.write_text(json.dumps({'self':'eliza','peer':'sondra','vault_name':'Memory',
            'vault_permissions':{'Memory':{'eliza':'write','sondra':'read'}},
            'transport_role':'host'}))
        original=topology.Topology(local=topology.Agent('Eliza','Commander',vault_access='write'),
            remote=[topology.Agent('Sondra','Lieutenant','localhost','Eliza','remote','read')])
        topology.save(original); before=topology.path().read_bytes(); config_before=conf_path.read_bytes()
        changed=topology.Topology(local=topology.Agent('Eliza','Reviewer',vault_access='write'),remote=original.remote)
        with patch.dict(os.environ, {'AGENTCOMM_CONF':str(conf_path), 'SKMR_AGENTS_PATH':str(topology.path()),
                'SKMR_MEMORY_PATH':str(native.memory_path()),'SKMR_CLAUDE_MD':str(config.path('claude_md'))}):
            roles=assign_role_cmd._roles()
            with patch.object(roles,'update_instruction_file',side_effect=OSError('write failed')):
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(assign_role_cmd._persist(changed,
                        {'Memory':{'eliza':'write','sondra':'read'}},{'eliza':'host','sondra':'consumer'}),1)
        self.assertEqual(topology.path().read_bytes(),before)
        self.assertEqual(conf_path.read_bytes(),config_before)
    def test_hackerone_latest_sorts_dates(self):
        from skmr.commands import hackerone_cmd
        dataset=self.root/'reports.json'
        dataset.write_text(json.dumps({'reports':[
            {'id':'1','attributes':{'title':'Old fixture','disclosed_at':'2020-01-01'}},
            {'id':'2','attributes':{'title':'New fixture','disclosed_at':'2026-01-01'}}]}))
        out=io.StringIO()
        with patch.object(hackerone_cmd,'DATASET',dataset),contextlib.redirect_stdout(out):
            self.assertEqual(hackerone_cmd.run(['latest','1'],self.plan()),0)
        self.assertIn('New fixture',out.getvalue());self.assertNotIn('Old fixture',out.getvalue())
    def test_config_invalid_types_degrade_per_key(self):
        conf=self.root/'bad_config.json';conf.write_text(json.dumps({'max_subagents':'many','chunk_max_chars':0,'search_mode':'unknown'}))
        with patch.object(config,'CONFIG_PATH',conf):
            loaded=config.load(refresh=True)
        self.assertEqual(loaded['max_subagents'],config.DEFAULTS['max_subagents'])
        self.assertEqual(loaded['chunk_max_chars'],config.DEFAULTS['chunk_max_chars'])
    def test_runtime_prevents_dependent_launch(self):
        from skmr.hooks import runtime
        orc.register(self.plan())
        decision=runtime.run_event({'hook_event_name':'PreToolUse','tool_name':'Agent','tool_input':{
            'prompt':'SKMR_AGENT_ID: verifier-agent\nverify results'}})
        self.assertEqual(decision['hookSpecificOutput']['permissionDecision'],'deny')
    def test_runtime_binds_real_agent_to_planned_work(self):
        from skmr.hooks import runtime
        plan=self.plan();orc.register(plan)
        prompt='SKMR_AGENT_ID: unit-01-agent\nalpha work'
        runtime.run_event({'hook_event_name':'PreToolUse','tool_name':'Agent','tool_input':{'prompt':prompt}})
        runtime.run_event({'hook_event_name':'SubagentStart','session_id':'test','agent_id':'abc','agent_type':'tester'})
        runtime.run_event({'hook_event_name':'PostToolUse','session_id':'test','tool_name':'Agent',
            'tool_input':{'prompt':prompt,'description':'alpha work'},'tool_response':{'agentId':'abc'}})
        runtime.run_event({'hook_event_name':'SubagentStop','session_id':'test','agent_id':'abc','last_assistant_message':
            json.dumps({'summary':'finished alpha fixture','findings':['fixture finding']})})
        results=orc.aggregate();self.assertIn('fixture finding',results['findings'])
        with orc._locked():entry=orc._load_results()['unit-01-agent']
        self.assertEqual(entry['status'],'COMPLETED');self.assertFalse(entry['verified'])

    def _dispatch_events(self,order):
        """Drive one planned agent through the hook events in the given delivery order."""
        from skmr.hooks import runtime
        orc.register(self.plan())
        prompt='SKMR_AGENT_ID: unit-01-agent\nalpha work'
        runtime.run_event({'hook_event_name':'PreToolUse','tool_name':'Agent','tool_input':{'prompt':prompt}})
        events={'start':{'hook_event_name':'SubagentStart','session_id':'test','agent_id':'abc','agent_type':'tester'},
            'post':{'hook_event_name':'PostToolUse','session_id':'test','tool_name':'Agent','tool_response':{'agentId':'abc'},
                'tool_input':{'prompt':prompt,'description':'alpha work','subagent_type':'tester'}},
            'stop':{'hook_event_name':'SubagentStop','session_id':'test','agent_id':'abc','last_assistant_message':
                json.dumps({'summary':'finished alpha fixture','findings':['fixture finding']})}}
        for key in order:runtime.run_event(events[key])
        with orc._locked():return orc._load_results()
    def test_late_subagent_start_still_binds_planned_agent(self):
        # PostToolUse may be delivered before SubagentStart; the planned agent must not stay QUEUED.
        data=self._dispatch_events(['post','start','stop'])
        self.assertEqual(data['unit-01-agent']['status'],'COMPLETED')
        self.assertEqual(data['unit-01-agent']['actual_id'],'9f86d08188-abc')
        self.assertEqual(data['9f86d08188-abc']['planned_id'],'unit-01-agent')
        self.assertIn('fixture finding',data['unit-01-agent']['findings'])
    def test_hook_delivery_order_does_not_change_outcome(self):
        outcomes=[]
        for order in [['start','post','stop'],['start','stop','post'],['post','start','stop']]:
            data=self._dispatch_events(order)
            outcomes.append({key:(entry['status'],entry.get('planned_id'),entry.get('actual_id')) for key,entry in data.items()})
            orc.clear()
        self.assertEqual(outcomes[0],outcomes[1]); self.assertEqual(outcomes[0],outcomes[2])
    def test_unplanned_agent_is_never_invented_from_tool_exit(self):
        from skmr.hooks import runtime
        orc.register(self.plan())
        runtime.run_event({'hook_event_name':'PostToolUse','session_id':'test','tool_name':'Agent',
            'tool_input':{'prompt':'no marker here','description':'stray work'},'tool_response':{'agentId':'zzz'}})
        with orc._locked():data=orc._load_results()
        self.assertNotIn('9f86d08188-zzz',data)

if __name__=='__main__': unittest.main(verbosity=2)
