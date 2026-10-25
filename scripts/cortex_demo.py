#!/usr/bin/env python3
"""Reproducible combined memory demonstration. MOCK only; no network/motor access."""
from __future__ import annotations
import argparse,json,os,subprocess,sys,tempfile,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from engram.cortex.store import Store
from engram.cortex.runtime import Brain
from engram.cortex.types import Policy
from engram.cortex.simulation import FakeRobot

def arena():return json.loads((ROOT/'fixtures/cortex/arena.json').read_text())
def phase(which,path):
    s=Store(path);s.register_arena(arena());b=Brain(s,FakeRobot(arena()))
    if which=='create':
        keep=b.schedule('experiment finished','Bring Sample A to bench',principal='demo')
        cancel=b.schedule('experiment finished','Bring Sample B to bench',principal='demo')
        b.cancel_task(cancel['task_id'],principal='demo')
        result={'waiting':keep['task_id'],'cancelled':cancel['task_id'],'pid':os.getpid()}
    else:
        a=b.trigger('experiment finished',event_id='instrument-test-1',principal='demo')
        c=b.trigger('experiment finished',event_id='instrument-test-1',principal='demo')
        assert len(a)==len(c)==1
        assert a[0]['proposal']['operation_id']==c[0]['proposal']['operation_id']
        tasks=s.rows('SELECT id,status FROM prospective_task')
        assert sorted(t['status'] for t in tasks)==['cancelled','fired'] and not b.robot.calls
        result={'tasks':tasks,'single_proposal':a[0]['proposal']['operation_id'],'duplicate_reused':True,'motor_calls':0,'pid':os.getpid()}
    b.close();return result

def run():
    report={'mode':'SIMULATED','actual_model_inference':False,'weight_training_performed':False,'checks':[]}
    def check(name,**evidence):report['checks'].append({'name':name,'pass':True,**evidence})
    with tempfile.TemporaryDirectory(prefix='engram-cortex-demo-') as temp:
        s=Store(Path(temp)/'memory.db',policy=Policy(auto_publish_verified=True,idle_after_s=.02));s.register_arena(arena());robot=FakeRobot(arena());b=Brain(s,robot)
        src=s.teach('SOP revision 1','Safety goggles must be at the ppe station before handling Sample B.',owner='demo')
        start=time.perf_counter();p=b.command('Bring Sample B to bench',principal='demo');first_ms=(time.perf_counter()-start)*1000
        recipe=s.derived(p['derived_memory_id']);assert recipe['status']=='VERIFIED' and recipe['payload']['steps']==p['plan']['steps']
        assert not robot.calls
        r=b.approve(p['operation_id'],p['digest'],principal='demo')
        assert r['status']=='success' and robot.positions['goggles']=='ppe_station' and robot.positions['sample_b']=='bench'
        check('new_task_uses_newly_stored_recipe',recipe_id=recipe['id'],source=src['source_id'],actual_mock_positions=dict(robot.positions),planning_ms=round(first_ms,3))
        # Controlled new-task fixture: put only the target back, then record a simulated observation.
        robot.positions['sample_b']='home';s.observe('sample_b','home',origin='demo_fixture_reset',mode='SIMULATED',evidence='controlled repeat fixture')
        start=time.perf_counter();again=b.command('Bring Sample B to bench',principal='demo');repeat_ms=(time.perf_counter()-start)*1000
        assert again['source']=='skill' and again['planner_calls']==0
        check('verified_skill_reuse',skill=again['skill_id'],planning_ms=round(repeat_ms,3),planner_calls=again['planner_calls'],note='Host timing only; not Pi performance and not a controlled speedup claim')
        revision=s.teach('SOP revision 2','Safety goggles must be at shelf B before handling Sample B.',owner='demo',supersedes=src['source_id'])
        assert b.operation(again['operation_id'])['status']=='invalidated'
        changed=b.command('Bring Sample B to bench',principal='demo')
        places=[x['args']['zone'] for x in changed['plan']['steps'] if x['op']=='place_at_zone']
        assert 'shelf_b' in places and changed['source']!='skill'
        check('source_correction_changes_same_request',new_source=revision['source_id'],new_destinations=places,old_skill_status=b.skills.get(again['skill_id'])['status'])
        for utterance in ['Do not move Sample A to bench','Bring Sample A to bench tomorrow']:
            assert b.command(utterance,principal='demo')['status']=='refused'
        n=s.one('SELECT COUNT(*) n FROM prospective_task')['n']
        assert b.command('After the experiment finished, what happened to Sample B?',principal='demo')['kind']=='answer'
        assert s.one('SELECT COUNT(*) n FROM prospective_task')['n']==n
        check('negation_future_and_retrospective_question')
        calls_before=len(robot.calls);b.dreamer.start(.02)
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            if s.one("SELECT COUNT(*) n FROM cx_dream WHERE status='complete'")['n']:break
            time.sleep(.02)
        assert b.dreamer.stop()
        assert s.one("SELECT COUNT(*) n FROM cx_dream WHERE status='complete'")['n']
        assert len(robot.calls)==calls_before
        check('automatic_idle_dream',derived=s.rows('SELECT kind,status,COUNT(*) n FROM cx_derived GROUP BY kind,status'),practice=s.rows('SELECT status,COUNT(*) n FROM cx_practice GROUP BY status'),new_motor_calls=0)
        memories=s.recall('What do you remember?')['memories'];assert len(memories)==7
        check('all_seven_memory_responsibilities',kinds=sorted(memories))
        b.close()
        path=str(Path(temp)/'restart.db')
        created=json.loads(subprocess.check_output([sys.executable,__file__,'--phase','create','--db',path],text=True))
        reopened=json.loads(subprocess.check_output([sys.executable,__file__,'--phase','resume','--db',path],text=True))
        assert created['pid']!=reopened['pid']
        check('real_process_restart_cancel_and_duplicate_trigger',created=created,reopened=reopened)
    report['all_passed']=all(c['pass'] for c in report['checks']);return report
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path);p.add_argument('--phase',choices=['create','resume']);p.add_argument('--db');a=p.parse_args()
    result=phase(a.phase,a.db) if a.phase else run()
    body=json.dumps(result,indent=2)
    if a.out:a.out.parent.mkdir(parents=True,exist_ok=True);a.out.write_text(body+'\n')
    print(body)
