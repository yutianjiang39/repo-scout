"""Offline fixture collector. Ephemeral keys; never inherits operator trust or credentials."""
import base64
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import review_gate as gate
from .common import canonical, now


class FixtureCollector:
    def __init__(self,store):
        self.store = store
        self.keys = {r:Ed25519PrivateKey.generate() for r in (*gate.ROLES,'human')}
        store.review_trust = {r:{'public_key':base64.b64encode(k.public_key().public_bytes_raw()).decode(),
                              'provider':'offline-fixture','model':r,'model_version':'fixture-1','canonical_model_id':'fixture/'+r}
                              for r,k in self.keys.items()}

    def sign(self,role,data):
        return base64.b64encode(self.keys[role].sign(canonical(data).encode())).decode()

    def report(self,subject,kind,role,phase,verdict='PASS',disagreements=None):
        b = gate.packet(self.store,subject,kind,role,phase)['binding']
        data = {'binding':b,'decision':{'session_id':uuid.uuid4().hex,'verdict':verdict,'evidence':[{'kind':'SOURCE_CONFIRMED','reference':'offline fixture source'}],
                                      'disagreements':disagreements or [],'rationale':'simulated review','reviewed_at':now()}}
        return {**data,'signature':self.sign(role,data)}

    def review(self,subject,kind):
        gate.start(self.store,subject,kind)
        for phase in ('independent','final'):
            for role in gate.ROLES:
                gate.receive(self.store,subject,kind,self.report(subject,kind,role,phase))

    def approve(self,fn,*args,**kwargs):
        c = fn(*args,**kwargs)['challenge']
        return fn(*args,**kwargs,receipt={'challenge':c,'signature':self.sign('human',c)})
