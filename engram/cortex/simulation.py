"""Explicit fake adapter for demonstrations/tests. Never imported as real hardware."""
from dataclasses import dataclass, field

@dataclass
class Result:
    status: str
    detail: str=''
    observations: list=field(default_factory=list)

class FakeRobot:
    def __init__(self,arena):
        self.positions={o['id']:o['zone'] for o in arena['objects']}
        self.tags={o['id']:o['tag'] for o in arena['objects']}
        self.at=arena.get('start_zone','home'); self.holding=None; self.open=True; self.stopped=False
        self.calls=[]; self.faults={}
    def state(self):
        return {'adapter':'mock','objects':dict(self.positions),'at_zone':self.at,'holding':self.holding,'gripper_open':self.open,'stopped':self.stopped}
    def stop(self): self.stopped=True
    def reset_stop(self): self.stopped=False
    def execute(self,op,args):
        self.calls.append((op,dict(args)))
        if op=='stop': self.stop(); return Result('done')
        if self.stopped: return Result('failed','stopped')
        if op in self.faults:
            f=self.faults.pop(op)
            if isinstance(f,Exception): raise f
            return Result(f,'injected outcome')
        if op=='observe': return Result('done','simulated view',[(self.tags[o],z) for o,z in self.positions.items()])
        if op=='locate_tag':
            o=args['object_id']
            return Result('done','simulated tag',[(self.tags[o],self.positions[o])]) if o in self.positions else Result('failed','not visible')
        if op=='pick_tagged_object':
            o=args['object_id']
            if self.holding or o not in self.positions: return Result('failed','cannot pick')
            self.at=self.positions.pop(o); self.holding=o; self.open=False
            return Result('done','simulated pickup')
        if op=='place_at_zone':
            if not self.holding: return Result('failed','empty claw')
            o,self.holding=self.holding,None; self.at=args['zone']; self.positions[o]=self.at; self.open=True
            return Result('done','simulated placement',[(self.tags[o],self.at)])
        if op=='go_to_zone': self.at=args['zone']; return Result('done','simulated navigation')
        if op=='gripper_open': self.open=True; return Result('done','simulated claw')
        if op=='gripper_close': self.open=False; return Result('done','simulated claw')
        if op in {'drive_bounded','turn_bounded','approach_tag','speak'}: return Result('done','simulated primitive')
        return Result('failed','unsupported primitive')
