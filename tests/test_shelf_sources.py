import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from repo_scout.database import Store
from repo_scout.shelf_sources import source_rows


def test_empty_home_and_uninitialized_database(tmp_path):
    home = tmp_path / 'missing'
    assert list(source_rows(home, None)) == []
    with unittest.TestCase().assertRaisesRegex(ValueError,'Pilot database not found'):
        list(source_rows(home, tmp_path/'missing-pilot'))
    assert not list(tmp_path.iterdir())
    db = home / 'data' / 'contribution.db'
    db.parent.mkdir(parents=True)
    db.touch()
    assert list(source_rows(home, None)) == []
    assert db.read_bytes() == b''


def test_workflow_projection(tmp_path):
    repo = 'org/project'
    secret = 'ghp_' + 'x' * 30
    private = 'PRIVATE_APPROVAL_PAYLOAD'
    with Store(tmp_path / 'data' / 'contribution.db') as store:
        store.put('repository', repo, {'repository': repo, 'status': 'APPROVED',
                                       'approval_reason': private}, reason=private)
        f = {'repository': repo, 'title': 'Fix ordering', 'status': 'CANDIDATE', 'last_patch': 'fix.diff'}
        store.put('finding', 'f', f, reason='imported')
        store.put('finding', 'f', dict(f, status='CONFIRMED', implementation_payload=private),
                  reason='checked ' + secret, actor=private, evidence=[private])
        store.put('draft', 'f', {'draft': {'title': 'Ordered results', 'body': private}})
        store.put('test_run', 'run', {'finding_id': 'f', 'status': 'PASS', 'log_path': 'output.log'})
        for kind, n in [('PR', 42), ('ISSUE', 8)]:
            store.put('submission', kind, {'finding_id': 'f', 'submission_type': kind,
                                           'number': n, 'status': 'OPEN', 'submission_url': secret})
        store.put('feedback', 'comment', {'submission_id': 'PR', 'type': 'QUESTION', 'body': private,
                                          'url': f'https://github.com/{repo}/pull/42?token={private}#discussion'})
        store.approve('implementation', 'f', private, actor=private, reason=private)
        store.connection.execute('INSERT INTO review_rounds VALUES(1,?,?,?,?,?)',
                                 ('round', 'f', 'diff', private, private))
        decision = {'verdict': 'PASS', 'reviewed_at': '2026-01-01', 'rationale': private}
        store.connection.execute('INSERT INTO review_receipts VALUES(?,?,?,?,?)',
                                 ('round', 'reviewer_a', 'final', private, json.dumps({'decision': decision})))

    rows = list(source_rows(tmp_path, None))
    assert {r['repository'] for r in rows} == {repo}
    assert {r['source'] for r in rows} == {'workflow'}
    by_kind = {r['kind']: r for r in rows}
    assert set(by_kind) == {'repository', 'finding', 'draft', 'test', 'issue', 'pr', 'feedback', 'review'}
    assert by_kind['pr']['key'] == '42' and by_kind['issue']['key'] == '8'
    assert by_kind['pr']['url'] == f'https://github.com/{repo}/pull/42'
    assert by_kind['feedback']['url'].endswith('/pull/42#discussion')
    assert Path(by_kind['draft']['material']) == tmp_path / 'reports/submissions/f/draft.md'
    assert by_kind['test']['material'] == 'output.log'
    assert by_kind['finding']['material'] == 'fix.diff'
    events = by_kind['finding']['events']
    assert [e['status'] for e in events] == ['CANDIDATE', 'CONFIRMED']
    assert events[-1]['at'] == by_kind['finding']['observed_at']
    assert events[-1]['reason'] == 'checked [REDACTED]'
    assert by_kind['review']['status'] == 'PASS'
    assert private not in repr(rows) and secret not in repr(rows)


def test_read_snapshot_spans_record_and_event_queries(tmp_path):
    db = tmp_path / 'data' / 'contribution.db'
    connect = sqlite3.connect
    with Store(db) as writer:
        f = {'repository': 'a/b', 'title': 'Title', 'status': 'OLD'}
        writer.put('finding', 'f', f)

        def open_reader(path, **kwargs):
            assert path.endswith('?mode=ro') and kwargs['uri']
            reader = connect(path, **kwargs)

            def concurrent_write(sql):
                if 'FROM events' in sql:
                    writer.put('finding', 'f', dict(f, status='NEW'))

            reader.set_trace_callback(concurrent_write)
            return reader

        with patch('repo_scout.shelf_sources.sqlite3.connect', open_reader):
            row, = source_rows(tmp_path, None)
        assert writer.require('finding', 'f')['status'] == 'NEW'
        assert row['status'] == 'OLD' and [e['status'] for e in row['events']] == ['OLD']


def test_pilot_directory_or_file_without_workflow(tmp_path):
    pilot = tmp_path / 'pilot #1'
    pilot.mkdir()
    db = pilot / 'pilot.sqlite3'
    with closing(sqlite3.connect(db)) as conn:
        conn.execute('CREATE TABLE scans(repo, sha, day, docs)')
        conn.execute("INSERT INTO scans VALUES('x/y','abc','2026-01-02',3)")
        conn.commit()
    before = db.read_bytes()
    rows = list(source_rows(tmp_path, pilot))
    assert rows == list(source_rows(tmp_path, db))
    scan, = rows
    assert (scan['source'], scan['repository'], scan['kind'], scan['key'], scan['observed_at']) == (
        'pilot', 'x/y', 'scan', 'abc', '2026-01-02')
    assert db.read_bytes() == before


def load_tests(loader, suite, pattern):
    # The project's standard runner uses unittest discovery.
    for func in (test_empty_home_and_uninitialized_database,test_workflow_projection,
                 test_read_snapshot_spans_record_and_event_queries,test_pilot_directory_or_file_without_workflow):
        def run(f=func):
            with tempfile.TemporaryDirectory() as tmp:
                f(Path(tmp))
        suite.addTest(unittest.FunctionTestCase(run,description=func.__name__))
    return suite
