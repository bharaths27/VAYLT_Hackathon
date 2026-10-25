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
    ('What do you see?',REFUSE),          # a question first: never matched against the observe phrases
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
    ('Look around',('primitive',(),None)),
]

@pytest.mark.parametrize('request_text,expected',TABLE)
def test_request_grammar_eval_table(store,request_text,expected):
    if expected is REFUSE:
        with pytest.raises(Missing): parse_goal(request_text,store)
    else:
        g=parse_goal(request_text,store)
        assert (g.kind,tuple(g.targets),g.zone)==expected

@pytest.mark.parametrize('request_text,named',[
    ('Bring Sample A but not Sample B to the bench','exclusion'),('Bring Sample A to the bench without the goggles','exclusion'),
    ('Take Sample A off the bench','source location'),('Take Sample B out of the ppe station','source location'),
    ('Bring Sample A or Sample B to the bench','alternatives'),('Bring Sample A to the bench if it is clean','condition'),
    ('Put Sample A next to the bench','relative position'),('Bring Sample A to the bench and then open the claw','sequence'),
    ('Should I bring Sample A to the bench?','question'),('Cancel that: bring Sample A to the bench','cancelled')])
def test_clarification_names_what_was_not_guessed(store,request_text,named):
    with pytest.raises(Missing,match=named): parse_goal(request_text,store)

def test_eval_table_is_large_enough_and_balanced():
    assert len(TABLE)>=25 and sum(e is REFUSE for _,e in TABLE)>=20 and sum(e is not REFUSE for _,e in TABLE)>=10

# On the command path a question is answered from memory (needle benchmark), still with nothing proposed; when only
# the "?" made it a question, the answer keeps the hint to say it as an instruction.
# A stop word wins before anything else (the brain's and console's vocabulary): these utterances stop the robot.
STOPPED={'Wait, hold on, put Sample A on the bench','Bring Sample A to the bench please, no wait'}
ANSWERED={'What do you see?':False,'Should I bring Sample A to the bench?':False,'Can I move the tray to shelf B':False,'Bring Sample A to the bench?':True,'Open the claw?':True}

@pytest.mark.parametrize('request_text',[r for r,e in TABLE if e is REFUSE])
def test_refused_request_creates_no_proposal_and_explains(brain,request_text):
    out=brain.command(request_text,principal='carther')
    if request_text in STOPPED:
        assert out['kind']=='stop' and brain._stopped.is_set()
    elif request_text in ANSWERED:
        assert out['kind']=='answer' and out['reply'] and ('without the question mark' in out['reply'])==ANSWERED[request_text]
    else:
        assert out['status']=='refused' and out['reason'] and out.get('kind')!='scheduled'
    assert brain.store.one('SELECT COUNT(*) n FROM episode')['n']==0 and not brain.robot.calls
