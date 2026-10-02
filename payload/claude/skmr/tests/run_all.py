#!/usr/bin/env python3
"""Run current SKMR suites with isolated config and runtime stores."""
import json, os, pathlib, shutil, subprocess, sys, tempfile, time
# Resolve the tree from this file's own location, not from a literal, so an
# installation under a different prefix tests ITSELF. A hardcoded /root/.claude
# made the installed runner test the machine the package was built on: the
# probe install reported failures that belonged to the authoring host.
HERE=pathlib.Path(__file__).resolve().parent; BASE=HERE.parents[1]
AGENTCOMM=pathlib.Path(os.environ.get('AGENTCOMM_CODE_ROOT',str(BASE.parent/'agentcomm')))
if not (AGENTCOMM/'test_agentcomm.py').exists() and pathlib.Path('/root/agentcomm').is_dir():
    AGENTCOMM=pathlib.Path('/root/agentcomm')
MEMORY=pathlib.Path(os.environ.get('SKMR_MEMORY_PATH',str(BASE/'projects/-root/memory/MEMORY.md')))
PYTHON=os.environ.get('SKMR_PYTHON',sys.executable or '/usr/bin/python3')
SKMR_SUITES=[('system',HERE/'test_system.py'),('failures',HERE/'test_failures.py'),
    ('index',HERE/'test_index.py'),('policy coverage',HERE/'test_policy_coverage.py'),
    ('audit regressions',HERE/'test_audit_regressions.py'),('report offers',HERE/'test_report_offer.py'),
    ('role permissions',HERE/'test_role_permissions.py')]
EXTERNAL_SUITES=[('agentcomm',AGENTCOMM/'test_agentcomm.py'),
    ('agentcomm symmetry',AGENTCOMM/'test_symmetry.py'),
    ('wire symmetry',AGENTCOMM/'test_wire_symmetry.py'),
    *[(p.stem,p) for p in sorted((BASE/'tests').glob('test_*.py'))]]
def main():
    suites=SKMR_SUITES if '--skmr-only' in sys.argv else SKMR_SUITES+EXTERNAL_SUITES
    passed=0
    for name,path in suites:
        start=time.monotonic()
        with tempfile.TemporaryDirectory(prefix='skmr-suite-') as directory:
            root=pathlib.Path(directory); conf=json.loads((BASE/'skmr/config/skmr.json').read_text())
            conf.update(state_dir=str(root/'state'),memory_path=str(root/'MEMORY.md'),
                agents_path=str(root/'agents.json'),claude_md=str(root/'CLAUDE.md'))
            for source,dest in [(BASE/'CLAUDE.md',root/'CLAUDE.md'),(BASE/'skmr/state/agents.json',root/'agents.json'),
                (MEMORY,root/'MEMORY.md')]:
                if source.exists():shutil.copy2(source,dest)
            config_path=root/'skmr.json'; config_path.write_text(json.dumps(conf))
            env=dict(os.environ,SKMR_CONFIG=str(config_path),SKMR_RUNTIME_STATE=conf['state_dir'],
                SKMR_MEMORY_PATH=conf['memory_path'],SKMR_AGENTS_PATH=conf['agents_path'],
                SKMR_CLAUDE_MD=conf['claude_md'],PYTHONDONTWRITEBYTECODE='1',SKMR_DISABLE_INBOX='1')
            try:
                result=subprocess.run([PYTHON,str(path)],cwd=str(path.parent),env=env,
                    text=True,capture_output=True,timeout=180)
                ok=result.returncode==0
                print(f'[{"PASS" if ok else "FAIL"}] {name}: {time.monotonic()-start:.2f}s',flush=True)
                if not ok: print((result.stdout+result.stderr)[-9000:],flush=True)
                passed+=ok
            except subprocess.TimeoutExpired:print(f'[TIMEOUT] {name}',flush=True)
    print(f'{passed}/{len(suites)} suites passed',flush=True)
    return 0 if passed==len(suites) else 1
if __name__=='__main__':raise SystemExit(main())
