import copy
import pytest
from engram.cortex.types import Policy
from engram.cortex.store import Store
from engram.cortex.runtime import Brain
from engram.cortex.simulation import FakeRobot

ARENA={'zones':['home','bench','ppe_station','shelf_b'], 'zone_tags':{'10':'home','11':'bench','12':'ppe_station','13':'shelf_b'},
       'objects':[{'id':'sample_a','name':'Sample A','tag':1,'zone':'home'},
                  {'id':'sample_b','name':'Sample B','tag':2,'zone':'home'},
                  {'id':'goggles','name':'Safety goggles','aliases':['goggles'],'tag':3,'zone':'home'},
                  {'id':'tray','name':'Tray','tag':4,'zone':'home'}]}
class Clock:
    def __init__(self): self.t=1700000000.0
    def __call__(self): return self.t
    def advance(self,s): self.t+=s
@pytest.fixture
def clock(): return Clock()
@pytest.fixture
def arena(): return copy.deepcopy(ARENA)
@pytest.fixture
def store(arena,clock):
    s=Store(clock=clock); s.register_arena(arena)
    yield s
    s.close()
@pytest.fixture
def brain(arena,clock):
    s=Store(clock=clock); s.register_arena(arena)
    b=Brain(s,FakeRobot(arena))
    yield b
    b.close()
@pytest.fixture
def rule(store):
    return store.teach('Lab rule','Safety goggles must be at the ppe station before handling Sample B.',owner='carther')
