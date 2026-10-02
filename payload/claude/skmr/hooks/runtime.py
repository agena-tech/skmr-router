"""Claude hook adapter: actual skill/agent events, state recall and identity.

Hooks supply plans to the parent; only Claude's Agent tool launches agents.
Start/stop events record actual agents and never treat a loaded Skill as finished work.
"""
from __future__ import annotations
import contextlib, hashlib, io, json, re
from ..core import output, registry
from ..memory import native, intent
from ..planning import planner
from ..agents import orchestrator as orc, topology

def _context(event_name,text):
    return {'hookSpecificOutput':{'hookEventName':event_name,'additionalContext':text}}
def monitored(name):
    folded=name.casefold()
    if folded.startswith(('skmr:','/skmr:')):return False
    if folded in {'skmr','send','obsidian-memory','claude-bug-hunter'} or folded.startswith(('send:','claude-bughunter:','hackerone-intelligence','bugbountyskills')):return True
    try:
        import sys
        from pathlib import Path
        directory=str(Path(__file__).resolve().parents[2]/'lib')
        if directory not in sys.path:sys.path.insert(0,directory)
        from skmr_security_registry import is_security_skill
        return is_security_skill(name)
    except (ImportError,OSError):return False
def describe(name):
    hunting=name.casefold().startswith(('claude-bughunter:','claude-bug-hunter'))
    return registry.Skill(name,f'External skill {name}','external:none',planning='medium' if hunting else 'simple',subagents=hunting)
def lifecycle(name,task):
    from ..hooks import planning
    out=io.StringIO()
    with contextlib.redirect_stdout(out):
        output.executing(name); plan=planning.run(describe(name),task)
    return out.getvalue().rstrip()
def _tag(event):return hashlib.sha256(str(event.get('session_id') or 'unknown').encode()).hexdigest()[:10]
def _actual_id(event):
    value=str(event.get('agent_id') or '')
    return _tag(event)+'-'+value if value else ''
def _metadata(event):
    inputs=event.get('tool_input') or {}
    return inputs if isinstance(inputs,dict) else {}
def _snapshot():
    state=native.load()
    parts=[f'## {name}\n{state.get(name)}' for name in native.MANAGED if state.get(name).strip()]
    if state.get('Subagent Details'):parts.append('## Subagent Details\n'+state.get('Subagent Details'))
    return '\n\n'.join(parts)[:12000] or 'No working state recorded yet.'
def _identity_context(detection):
    current=topology.load()
    text=topology.render_markdown(current)
    if detection.intent==intent.IDENTITY_USER:
        # User facts belong to the profile, never to this machine's runtime state.
        text+='\nUser identity: consult the smallest canonical User Profile note via /skmr:obsidian-memory search --profile. Do not infer the user from the local agent identity.'
    return text
def _record_actual(agent_id,role,task):
    """Register an actual Claude agent SKMR has not seen yet. Never call while holding the store lock."""
    orc.register(planner.Plan('actual-agent',task,'simple','Collect and independently verify the agent result',
        subagents=[planner.SubagentSpec(agent_id,role,task,'Structured result from actual Claude agent')]))
def _run_event(event):
    if not isinstance(event,dict):return None
    kind=str(event.get('hook_event_name') or 'PreToolUse'); tool=str(event.get('tool_name') or '')
    inputs=_metadata(event)
    if kind=='SessionStart':
        current=topology.load()
        return _context(kind,'Persisted SKMR identity (agent topology source of truth):\n'+topology.render_markdown(current))
    if kind=='UserPromptSubmit':
        prompt=str(event.get('prompt') or '')
        if prompt.lstrip().startswith('/'):return None
        detection=intent.detect(prompt,allow_semantic=False)
        if detection is None:
            # Cheap lexical path first. Limit semantic fallback to short questions;
            # never embed a long ordinary task or raw tool output.
            if '?' not in prompt or len(prompt)>220:return None
            from ..memory.retrieval import embeddings
            try:
                engine=embeddings.provider(); old_timeout=engine.timeout; engine.timeout=min(old_timeout,2)
                try:detection=intent.detect(prompt,allow_semantic=True)
                finally:engine.timeout=old_timeout
            except Exception:return None
        if detection is None:return None
        body=_identity_context(detection) if detection.is_identity else _snapshot()
        route='identity' if detection.is_identity else 'native-memory'
        return _context(kind,f'[SKMR]: Routing: "{route}" ...\n'+body+'\nUse this persisted context; preserve unknown details as unknown.')
    if tool=='Skill' and kind in {'PreToolUse','PostToolUse','PostToolUseFailure'}:
        name=str(inputs.get('skill') or inputs.get('name') or '')
        if not monitored(name):return None
        task=str(inputs.get('args') or '')
        if kind=='PreToolUse':
            plan=planner.build(describe(name),task)
            tag=_tag(event)
            remap={s.agent_id:tag+'-'+s.agent_id for s in plan.subagents}
            for spec in plan.subagents:
                spec.agent_id=remap[spec.agent_id]; spec.depends_on=tuple(remap[d] for d in spec.depends_on)
            native.mutate(lambda state:(state.set('Active Task',task or name),state.set('Phase','Planning '+name),
                state.set('Current Work','Load skill instructions, then execute the approved plan.'),state.set('Next','Execute and verify the result.')))
            text=lifecycle(name,task)
            if plan.subagents:
                orc.register(plan)
                text+='\nDispatch only independent PLANNED agents using the Agent tool; include SKMR_AGENT_ID: <id> in each agent prompt. '+\
                    'Read /skmr:state agents for the scoped IDs and dependencies. Wait for COMPLETED prerequisites before dispatching dependents. '+\
                    'Return structured agent/task/status/summary/findings/artifacts/open_problems/next_step results. Verify critical claims independently; '+\
                    'a process finishing is not evidence that its findings are correct.'
            return _context(kind,text)
        if kind=='PostToolUseFailure':
            native.mutate(lambda state:(state.set('Phase','Skill load failed'),state.set('Current Work',name),
                state.set('Next','Diagnose the skill failure before retrying.')))
        else:
            native.mutate(lambda state:state.set('Phase','Skill instructions loaded; execution and verification pending'))
        return None
    if kind=='PreToolUse' and tool in {'Agent','Task'}:
        prompt=str(inputs.get('prompt') or '')
        match=re.search(r'SKMR_AGENT_ID:\s*([A-Za-z0-9_.-]+)',prompt)
        if match:
            agent_id=match.group(1)
            with orc._locked():
                data=orc._load_results()
                try:orc._check(data,agent_id,orc.RUNNING)
                except orc.TransitionError as exc:
                    return {'hookSpecificOutput':{'hookEventName':kind,'permissionDecision':'deny','permissionDecisionReason':str(exc)}}
            # Queue the request; SubagentStart is the evidence that it actually ran.
            entry=data[agent_id]
            if entry['status']==orc.PLANNED:orc.transition(agent_id,orc.QUEUED,'Dispatch requested; awaiting actual start')
        return None
    if kind=='PostToolUse' and tool in {'Agent','Task'}:
        response=event.get('tool_response') or {}
        if not isinstance(response,dict):return None
        raw_id=str(response.get('agentId') or response.get('agent_id') or '')
        if not raw_id:return None  # never invent process identity from a successful tool exit
        actual=_tag(event)+'-'+raw_id
        match=re.search(r'SKMR_AGENT_ID:\s*([A-Za-z0-9_.-]+)',str(inputs.get('prompt') or ''))
        planned=match.group(1) if match else ''
        role=str(inputs.get('subagent_type') or 'Subagent')
        with orc._locked():
            data=orc._load_results()
            if actual not in data:
                # Hook delivery order is not guaranteed; this may arrive before SubagentStart.
                if not planned or planned not in data:return None
                pending=data[planned]['task']
            else:pending=None
        if pending is not None:_record_actual(actual,role,pending)
        with orc._locked():
            data=orc._load_results(); entry=data.get(actual)
            if not entry:return None
            entry['task']=str(inputs.get('description') or inputs.get('prompt') or entry['task'])[:500]
            if planned:
                if planned not in data:
                    orc._save_results(data); return None
                entry['planned_id']=planned; data[planned]['actual_id']=actual
            orc._save_results(data)
        if planned:
            if str(data[planned].get('status','')).upper() not in orc.TERMINAL:
                orc.transition(planned,orc.RUNNING,'Actual agent confirmed: '+actual)
            if entry['status'] in {orc.COMPLETED,orc.BLOCKED,orc.FAILED,orc.CANCELLED}:
                orc.record(orc.Result(planned,data[planned]['task'],entry['status'],summary=entry.get('summary',''),
                    findings=entry.get('findings',[]),artifacts=entry.get('artifacts',[]),
                    open_problems=entry.get('open_problems',[]),next_step=entry.get('next_step',''),verified=False))
        return None
    if kind in {'SubagentStart','SubagentStop'}:
        agent_id=_actual_id(event)
        if not agent_id:return None
        role=str(event.get('agent_type') or 'Subagent')
        if kind=='SubagentStart':
            with orc._locked():entry=orc._load_results().get(agent_id)
            if entry is None:
                _record_actual(agent_id,role,native.load().get('Active Task') or role)
            elif str(entry.get('status','')).upper() in orc.TERMINAL:
                # PostToolUse already closed this agent out; do not reopen a terminal record.
                return _context(kind,f'SKMR already tracks this actual agent as {agent_id}.')
            orc.transition(agent_id,orc.RUNNING,'Actual SubagentStart event received')
            return _context(kind,f'SKMR tracks this actual agent as {agent_id}. Return a structured result; preserve blockers and dependencies. '+
                'Do not overwrite MEMORY.md directly; use /skmr:state agent/result to report milestones.')
        with orc._locked():entry=orc._load_results().get(agent_id)
        if not entry:return None  # internal/unknown agents are not invented as completed SKMR work
        message=str(event.get('last_assistant_message') or '')
        data={}
        try:
            parsed=json.loads(message)
            if isinstance(parsed,dict):data=parsed
        except ValueError:pass
        status=str(data.get('status') or 'COMPLETED').upper()
        if status not in {'COMPLETED','BLOCKED','FAILED','CANCELLED'}:status='COMPLETED'
        result=orc.Result(agent_id,entry['task'],status,summary=str(data.get('summary') or message)[:800],
            findings=[str(v)[:300] for v in data.get('findings',[])[:20]] if isinstance(data.get('findings'),list) else [],
            artifacts=[str(v)[:300] for v in data.get('artifacts',[])[:10]] if isinstance(data.get('artifacts'),list) else [],
            open_problems=[str(v)[:300] for v in data.get('open_problems',[])[:10]] if isinstance(data.get('open_problems'),list) else [],
            next_step=str(data.get('next_step') or 'Parent must verify critical findings before acceptance.')[:300],verified=False)
        orc.record(result)
        planned=entry.get('planned_id')
        if planned:
            with orc._locked():planned_entry=orc._load_results().get(planned)
            if planned_entry and planned_entry.get('status')==orc.RUNNING:
                result.agent_id=planned; result.task=planned_entry['task']; orc.record(result)
        return _context(kind,'Actual SKMR subagent result recorded in MEMORY.md. Critical claims remain unverified until the parent checks evidence and tests.')
    return None


def run_event(event):
    from . import report_review
    if not isinstance(event,dict):return None
    kind=str(event.get('hook_event_name') or 'PreToolUse')
    if kind=='SessionStart':report_review.reset(event)
    addition=report_review.on_prompt(event) if kind=='UserPromptSubmit' else ''
    reminder=_read_delegation_reminder(event)
    addition='\n\n'.join(part for part in [addition,reminder] if part)
    result=_run_event(event)
    if not addition:return result
    previous=(result or {}).get('hookSpecificOutput',{}).get('additionalContext','')
    return _context(kind,'\n\n'.join(part for part in [addition,previous] if part))


def _read_delegation_reminder(event):
    """Small event-specific guidance only; never read or consume an inbox."""
    kind=str(event.get('hook_event_name') or '')
    if kind not in {'SessionStart','PostToolUse'}:return ''
    if kind=='PostToolUse':
        inputs=_metadata(event)
        name=str(inputs.get('skill') or inputs.get('name') or '')
        if event.get('tool_name')!='Skill' or not monitored(name):return ''
    try:
        import os, sys
        from pathlib import Path
        directory=os.environ.get('AGENTCOMM_LIB',str(Path(__file__).resolve().parents[2]/'lib'))
        if directory not in sys.path:sys.path.insert(0,directory)
        from skmr_permissions import can_write, config_path
        conf=json.loads(config_path().read_text())
        if can_write(conf=conf):return ''
        peer=str(conf.get('peer') or '')
        recipient=peer if peer and can_write(peer,conf=conf) else 'the configured WRITE agent'
    except (ImportError,OSError,ValueError):return ''
    return (f'Assigned vault access is READ. When a concrete memory gap or verified security outcome yields a novel reusable lesson, '
            f'perform only narrow relevant recall, stage the smallest evidence-backed candidate and delegate it to {recipient} with '
            '/skmr:send candidate <file>. Candidate delivery is not a save; report saved only after a confirmed canonical commit. '
            'Do not poll or consume the inbox here; asyncRewake handles incoming messages.')
