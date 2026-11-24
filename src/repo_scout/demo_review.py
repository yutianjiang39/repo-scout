"""Synthetic dual-review records for offline demos and tests only."""
import uuid


from . import review_gate as gate
from .common import now


class FixtureCollector:
    def __init__(self,store):
        self.store = store

    def report(self,subject,kind,role,phase,verdict='PASS',disagreements=None):
        b = gate.packet(self.store,subject,kind,role,phase)['binding']
        data = {'binding':b,'decision':{'session_id':uuid.uuid4().hex,'verdict':verdict,'evidence':[{'kind':'SOURCE_CONFIRMED','reference':'offline fixture source'}],
                                      'disagreements':disagreements or [],'rationale':'simulated review','reviewed_at':now()}}
        return data

    def review(self,subject,kind):
        gate.start(self.store,subject,kind)
        for phase in ('independent','final'):
            for role in gate.ROLES:
                gate.receive(self.store,subject,kind,self.report(subject,kind,role,phase))

    def approve(self,fn,*args,**kwargs):
        preview = fn(*args,**kwargs)
        return fn(*args,**kwargs,confirm_digest=preview['approval_digest'])
