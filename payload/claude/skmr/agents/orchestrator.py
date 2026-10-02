"""Locked subagent state, dependency gating, structured results and aggregation."""
from __future__ import annotations
import fcntl, json, os, time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from ..core import config, output
from ..memory import native
from ..memory.native import Subagent

PLANNED, QUEUED, RUNNING, BLOCKED, COMPLETED, FAILED, CANCELLED = (
    'PLANNED','QUEUED','RUNNING','BLOCKED','COMPLETED','FAILED','CANCELLED')
TERMINAL=frozenset({COMPLETED,FAILED,CANCELLED})
ALLOWED={PLANNED:frozenset({QUEUED,RUNNING,CANCELLED}),
    QUEUED:frozenset({RUNNING,BLOCKED,FAILED,CANCELLED}),
    RUNNING:frozenset({BLOCKED,COMPLETED,FAILED,CANCELLED}),
    BLOCKED:frozenset({RUNNING,FAILED,CANCELLED}),COMPLETED:frozenset(),
    FAILED:frozenset({QUEUED}),CANCELLED:frozenset({QUEUED})}
class TransitionError(ValueError): pass
@dataclass
class Result:
    agent_id:str
    task:str
    status:str
    summary:str=''
    findings:list[str]=field(default_factory=list)
    artifacts:list[str]=field(default_factory=list)
    open_problems:list[str]=field(default_factory=list)
    next_step:str=''
    verified:bool=False
    def as_block(self):
        return '\n'.join([f'Agent: {self.agent_id}',f'Task: {self.task}',f'Status: {self.status}',
            f'Summary: {self.summary or "-"}', 'Important Findings:',*[f'  - {x}' for x in self.findings or ['-']],
            'Artifacts:',*[f'  - {x}' for x in self.artifacts or ['-']],
            'Open Problems:',*[f'  - {x}' for x in self.open_problems or ['-']],f'Recommended Next Step: {self.next_step or "-"}'])
def results_path(): return config.state_dir()/'subagent-results.json'
@contextmanager
def _locked():
    with (config.state_dir()/'orchestrator.lock').open('a+') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        try: yield
        finally: fcntl.flock(handle,fcntl.LOCK_UN)
def _load_results():
    try:
        raw=json.loads(results_path().read_text())
        if not isinstance(raw,dict): raise TransitionError('subagent store is not an object')
        return raw
    except FileNotFoundError: return {}
    except json.JSONDecodeError as exc: raise TransitionError('corrupt subagent store; preserve and repair it before updating') from exc
def _save_results(data):
    target=results_path(); temp=target.with_suffix('.tmp')
    temp.write_text(json.dumps(data,indent=2,ensure_ascii=False)+'\n'); os.replace(temp,target)
def _details(state,data):
    if not data:
        state.drop('Subagent Details'); return
    lines=['| Agent | Role | Reason / Expected Output | Depends on | Findings | Remaining / Blockers |',
           '|---|---|---|---|---|---|']
    def cell(value): return ' '.join(str(value).splitlines()).replace('|','&#124;')[:300] or '-'
    for key,e in data.items():
        lines.append('| '+' | '.join(cell(x) for x in [key,e.get('role',''),e.get('expected_output',''),
            ', '.join(e.get('depends_on',[])), '; '.join(e.get('findings',[])),
            '; '.join(e.get('open_problems',[])) or e.get('next_step','') or (e.get('progress','') if e.get('status')==BLOCKED else '')])+' |')
    state.set('Subagent Details','\n'.join(lines))
def _validate_plan(plan):
    specs={s.agent_id:s for s in plan.subagents}
    if len(specs)!=len(plan.subagents): raise TransitionError('duplicate subagent identifiers')
    for s in plan.subagents:
        if not s.agent_id or not s.task.strip(): raise TransitionError('agent identifier and task are required')
        if s.agent_id in s.depends_on or any(d not in specs for d in s.depends_on):
            raise TransitionError('invalid subagent dependency')
    def walk(key,path):
        if key in path: raise TransitionError('subagent dependency cycle')
        for dep in specs[key].depends_on: walk(dep,path|{key})
    for key in specs: walk(key,set())
def register(plan):
    _validate_plan(plan)
    if not plan.subagents: return []
    with _locked():
        data=_load_results()
        for spec in plan.subagents:
            if spec.agent_id in data and data[spec.agent_id].get('status') not in TERMINAL:
                raise TransitionError(f'active subagent {spec.agent_id} already exists')
        rows=[]
        for spec in plan.subagents:
            status=QUEUED if spec.depends_on else PLANNED
            rows.append(Subagent(spec.agent_id,spec.task,status,''))
            data[spec.agent_id]={'agent_id':spec.agent_id,'role':spec.role,'task':spec.task,
                'expected_output':spec.expected_output,'depends_on':list(spec.depends_on),
                'status':status,'skill':plan.skill,'registered_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())}
        def apply(state):
            existing={r.agent_id:r for r in state.subagents()}
            existing.update({r.agent_id:r for r in rows})
            state.set('Active Task',plan.task or plan.skill); state.set('Phase',f'{plan.complexity} plan — awaiting dispatch')
            state.set('Next','Dispatch independent planned agents; wait for prerequisites before dependent work.')
            state.set_subagents(list(existing.values())); _details(state,data)
        native.mutate(apply); _save_results(data)
        return rows
def _check(data,agent_id,status):
    if status not in ALLOWED: raise TransitionError(f'unknown state {status}')
    entry=data.get(agent_id)
    if entry is None: raise TransitionError(f'unknown subagent {agent_id}')
    current=str(entry.get('status','')).upper()
    if current!=status and status not in ALLOWED.get(current,()):
        raise TransitionError(f'illegal transition {current} -> {status} for {agent_id}')
    if status==RUNNING:
        pending=[d for d in entry.get('depends_on',[]) if data.get(d,{}).get('status')!=COMPLETED]
        if pending: raise TransitionError(f'{agent_id} depends on {", ".join(pending)} which are not COMPLETED')
    return entry
def _legacy_rows(data):
    # Existing MEMORY.md is a supported migration source for an already tracked row.
    for row in native.load().subagents():
        if row.agent_id not in data:
            data[row.agent_id]={'agent_id':row.agent_id,'task':row.task,'status':row.status,'progress':row.progress,'depends_on':[]}
def transition(agent_id,status,progress=''):
    status=status.upper()
    with _locked():
        data=_load_results(); _legacy_rows(data); entry=_check(data,agent_id,status)
        entry['status']=status
        if progress: entry['progress']=progress
        holder={}
        def apply(state):
            row=Subagent(agent_id,entry['task'],status,entry.get('progress',''))
            state.upsert_subagent(row); _details(state,data); holder['row']=row
        native.mutate(apply); _save_results(data); return holder['row']
def record(result):
    status=result.status.upper()
    with _locked():
        data=_load_results(); entry=_check(data,result.agent_id,status)
        entry.update(asdict(result)); entry['status']=status
        entry['recorded_at']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
        def apply(state):
            state.upsert_subagent(Subagent(result.agent_id,entry['task'],status,result.summary[:160]))
            if result.open_problems:
                existing=[x for x in state.get('Open Problems').splitlines() if x.strip() not in {'','-'}]
                for problem in result.open_problems:
                    bullet=f'- {result.agent_id}: {problem}'
                    if bullet not in existing: existing.append(bullet)
                state.set('Open Problems','\n'.join(existing))
            if result.next_step: state.set('Next',result.next_step)
            _details(state,data)
        native.mutate(apply); _save_results(data)
def aggregate():
    with _locked(): data=_load_results()
    findings={}; unverified=[]; blocked=[]; failed=[]
    for agent_id,entry in data.items():
        status=str(entry.get('status','')).upper()
        if status==BLOCKED: blocked.append(agent_id)
        if status==FAILED: failed.append(agent_id)
        for item in entry.get('findings',[]) or []:
            key=' '.join(str(item).split())
            if key: findings.setdefault(key,[]).append(agent_id)
        if status==COMPLETED and not entry.get('verified'): unverified.append(agent_id)
    return {'agents':len(data),'findings':findings,'duplicated':{k:v for k,v in findings.items() if len(v)>1},
        'unverified':unverified,'blocked':blocked,'failed':failed}
def summary():
    rows=native.load().subagents()
    if not rows: output.info('No subagents registered.'); return
    output.table(list(native.SUBAGENT_COLUMNS),[row.row() for row in rows])
    merged=aggregate()
    if merged['duplicated']: output.info(f"{len(merged['duplicated'])} duplicate findings merged.")
    for key in ['blocked','failed','unverified']:
        if merged[key]: output.warning(key+': '+', '.join(merged[key]))
def clear():
    with _locked():
        native.mutate(lambda state:(state.set_subagents([]),state.drop('Subagent Details')))
        _save_results({})
