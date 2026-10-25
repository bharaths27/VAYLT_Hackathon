"""Eval table for the request grammar (P1 finding 2): refuse by default; positive controls still plan."""
import pytest
from engram.cortex.planning import parse_goal
from engram.cortex.types import Missing

F=lambda targets,zone:('fetch',tuple(targets),zone)
REFUSE=None
TABLE=[
    # negation / exclusion
    ('Bring Sample A but not Sample B to the bench',REFUSE),
    ('Bring Sample A to the bench, leave Sample B alone',REFUSE),
    ('Bring Sample A to the bench without the goggles',REFUSE),
    ('Bring everything except Sample B to the bench',REFUSE),
    ('Do not move Sample A to the bench',REFUSE),
    ('Actually, put Sample A on shelf B instead',REFUSE),
    # source prepositions
    ('Take Sample A off the bench',REFUSE),
    ('Move Sample A away from the bench',REFUSE),
    ('Take Sample B out of the ppe station',REFUSE),
    ('Move Sample A from home to the bench',REFUSE),
    # cancel / hold
    ('Cancel that: bring Sample A to the bench',REFUSE),
    ('Wait, hold on, put Sample A on the bench',REFUSE),
    ('Bring Sample A to the bench please, no wait',REFUSE),
    ('Never mind, bring Sample A to the bench',REFUSE),
    ('Scratch that, move the tray to the bench',REFUSE),
    # questions
    ('Should I bring Sample A to the bench?',REFUSE),
    ('Bring Sample A to the bench?',REFUSE),
    ('Can I move the tray to shelf B',REFUSE),
    ('Open the claw?',REFUSE),
    # alternatives, conditions, relative places, sequences, unknown words
    ('Bring Sample A or Sample B to the bench',REFUSE),
    ('Bring Sample A to the bench if it is clean',REFUSE),
    ('Put Sample A next to the bench',REFUSE),
    ('Bring Sample A to the bench and then open the claw',REFUSE),
    ('Pick up Sample A and put it on the bench',REFUSE),
    ('Bring Sample A and the red cup to the bench',REFUSE),
    ('Find the goggles on the bench',REFUSE),
    # positive controls: must still plan exactly this goal
    ('Bring Sample A to the bench',F(['sample_a'],'bench')),
    ('Bring Sample B to bench.',F(['sample_b'],'bench')),
    ('Put the tray on shelf B',F(['tray'],'shelf_b')),
    ('Take Sample A to the ppe station',F(['sample_a'],'ppe_station')),
    ('Carry the goggles to the bench',F(['goggles'],'bench')),
    ('Bring Sample A home',F(['sample_a'],'home')),
    ('Please bring Sample A to the bench',F(['sample_a'],'bench')),
    ('Could you bring Sample A to the bench',F(['sample_a'],'bench')),
    ('Bring the goggles and Sample B to the bench',F(['goggles','sample_b'],'bench')),
    ('Go to the bench',('goto',(),'bench')),
    ('Return home',('goto',(),'home')),
    ('Find the goggles',('inspect',('goggles',),None)),
    ('Open the claw',('primitive',(),None)),
    ('What do you see?',('primitive',(),None)),
]

@pytest.mark.parametrize('request_text,expected',TABLE)
def test_request_grammar_eval_table(store,request_text,expected):
    if expected is REFUSE:
        with pytest.raises(Missing): parse_goal(request_text,store)
    else:
        g=parse_goal(request_text,store)
        assert (g.kind,tuple(g.targets),g.zone)==expected

def test_eval_table_is_large_enough_and_balanced():
    assert len(TABLE)>=25 and sum(e is REFUSE for _,e in TABLE)>=20 and sum(e is not REFUSE for _,e in TABLE)>=10

@pytest.mark.parametrize('request_text',[r for r,e in TABLE if e is REFUSE])
def test_refused_request_creates_no_proposal_and_explains(brain,request_text):
    out=brain.command(request_text,principal='carther')
    assert out['status']=='refused' and out['reason'] and out.get('kind')!='scheduled'
    assert brain.store.one('SELECT COUNT(*) n FROM episode')['n']==0 and not brain.robot.calls
