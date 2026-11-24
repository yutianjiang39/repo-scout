import json
import sqlite3
from contextlib import closing
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from repo_scout import review_gate as gate
from repo_scout.database import Store
from repo_scout.demo_review import FixtureCollector


class StrictGateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name)/'test.db')
        self.addCleanup(self.store.close)
        self.fixture = FixtureCollector(self.store)
        self.subject = 'example/profile'
        self.args = self.store,self.subject,'profile'
        self.store.put('profile_draft',self.subject,{'draft':{'base_sha':'a'*40,'content':'example'}})
        self.rid = gate.start(*self.args)

    def send(self,role,phase,verdict='PASS',disagreements=None):
        r = self.fixture.report(self.subject,'profile',role,phase,verdict,disagreements)
        gate.receive(*self.args,r)
        return r

    def independent(self):
        for role in gate.ROLES:
            self.send(role,'independent')

    def test_blind_packet_does_not_exchange_early_or_include_first_opinion(self):
        self.send('reviewer_a','independent')
        self.assertNotIn('simulated review',json.dumps(gate.packet(*self.args,'reviewer_b','independent')))
        with self.assertRaises(ValueError): gate.packet(*self.args,'reviewer_b','final')

    def test_one_missing_final_in_either_direction_blocks(self):
        for who in gate.ROLES:
            gate.start(*self.args)
            self.independent()
            self.send(who,'final')
            with self.assertRaises(ValueError): gate.require(*self.args)

    def test_nonpass_and_unresolved_disagreement_block(self):
        for verdict,issues in [('REJECT',[]),('REVISE',[]),('BLOCKED',[]),('PASS',['missing guard'])]:
            gate.start(*self.args)
            self.independent()
            self.send('reviewer_a','final')
            self.send('reviewer_b','final',verdict,issues)
            with self.assertRaises(ValueError): gate.require(*self.args)

    def test_implementation_session_is_not_independent(self):
        r = self.fixture.report(self.subject,'profile','reviewer_a','independent')
        self.store.put('cc_session',r['decision']['session_id'],{'task':'implementation'})
        with self.assertRaises(ValueError): gate.receive(*self.args,r)

    def test_changed_binding_and_reused_session_block(self):
        r = self.send('reviewer_a','independent')
        with self.assertRaises(ValueError): gate.receive(*self.args,r)
        r = self.fixture.report(self.subject,'profile','reviewer_b','independent')
        r['binding']['artifact_digest']='b'*64
        with self.assertRaises(ValueError): gate.receive(*self.args,r)

    def test_submission_excludes_nested_implementer_and_registered_sessions(self):
        artifact = {'repository':self.subject, 'source_commit_sha':'a'*40,
                    'diff':{'implementer_session':'impl'}, 'evidence':{}}
        with patch.object(gate, 'artifact', return_value=artifact):
            gate.start(self.store,self.subject,'submission')
            self.store.put('cc_session','registered',{'task':'validation'})
            for session in ('impl','gpt:impl','gpt:registered'):
                report = self.fixture.report(self.subject,'submission','reviewer_a','independent')
                report['decision']['session_id'] = session
                with self.assertRaisesRegex(ValueError,'fresh independent'):
                    gate.receive(self.store,self.subject,'submission',report)

    def test_active_plan_cannot_lose_its_finding_round_to_review_start(self):
        self.store.put('finding',self.subject,{'status':'IMPLEMENTING'})
        with self.assertRaisesRegex(ValueError,'re-enter validation'):
            gate.start(self.store,self.subject,'finding')
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM review_rounds WHERE kind='finding'").fetchone()[0],0)

    def test_new_round_invalidates_old_pass_and_cannot_mix_finals(self):
        self.independent(); self.send('reviewer_a','final'); old=self.send('reviewer_b','final')
        self.assertTrue(gate.require(*self.args))
        gate.start(*self.args); self.independent(); self.send('reviewer_a','final')
        with self.assertRaises(ValueError): gate.receive(*self.args,old)
        with self.assertRaises(ValueError): gate.require(*self.args)

    def test_provider_prefix_does_not_create_a_new_session(self):
        for first, second in [('raw1','gpt:raw1'),('gpt:raw2','raw2')]:
            gate.start(*self.args)
            for index, session in enumerate((first,second)):
                role = 'reviewer_a'
                if index:
                    gate.start(*self.args)
                report = self.fixture.report(self.subject,'profile',role,'independent')
                report['decision']['session_id'] = session
                if index == 0:
                    gate.receive(*self.args,report)
                else:
                    with self.assertRaisesRegex(ValueError,'session already used'):
                        gate.receive(*self.args,report)

    def test_sha_or_content_changed_blocks_and_history_is_immutable(self):
        self.fixture.review(self.subject,'profile')
        for key in ('base_sha','content'):
            draft={'base_sha':'a'*40,'content':'example',key:'changed'}
            self.store.put('profile_draft',self.subject,{'draft':draft})
            with self.assertRaises(ValueError): gate.require(*self.args)
        for table in ('review_rounds','review_receipts'):
            with self.assertRaises(sqlite3.IntegrityError): self.store.connection.execute(f'DELETE FROM {table}')

    def test_preview_is_not_approval_and_stale_confirmation_is_rejected(self):
        args = self.store,'submission',self.subject
        payload = {'content':'draft'}
        preview = gate.human_gate(*args,payload,'operator','checked')
        with self.assertRaises(ValueError): gate.authorized(*args,payload)
        with self.assertRaises(ValueError):
            gate.human_gate(*args,{'content':'edited'},'operator','checked',preview['approval_digest'])
        gate.human_gate(*args,payload,'operator','checked',preview['approval_digest'])
        gate.authorized(*args,payload,consume=True)
        with self.assertRaises(ValueError): gate.authorized(*args,payload,consume=True)

    def test_temporary_v1_copy_migrates_without_promoting_old_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp)/'legacy.db'
            with closing(sqlite3.connect(db)) as c:
                c.executescript("CREATE TABLE schema_version(version INTEGER); INSERT INTO schema_version VALUES(1); CREATE TABLE records(kind TEXT,id TEXT,version INTEGER,status TEXT,data TEXT,updated_at TEXT,PRIMARY KEY(kind,id)); INSERT INTO records VALUES('finding','old',7,'CONFIRMED','{\"status\":\"CONFIRMED\"}','old');")
            with Store(db) as s:
                self.assertEqual(s.require('finding','old')['_version'],7)
                self.assertEqual(s.connection.execute('SELECT count(*) FROM review_rounds').fetchone()[0],0)
