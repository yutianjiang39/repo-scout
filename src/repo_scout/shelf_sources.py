"""Read local workflow and pilot observations without opening either writable store."""

import json
import re
import sqlite3
from collections import defaultdict
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .common import redact


_KINDS = ('repository', 'finding', 'submission', 'draft', 'validation', 'diff_review',
          'test_run', 'feedback', 'audit_run')
_SLUG = re.compile(r'[\w.-]+/[\w.-]+', re.ASCII)


def _text(value):
    text = redact(str(value))
    text = re.sub(r'(?i)(https?://)[^/\s]+@', r'\1[REDACTED]@', text)
    return re.sub(r'(?i)\b(token|access_token|api[_-]?key|password|secret)=([^\s&]+)',
                  r'\1=[REDACTED]', text)


def _row(repo, kind, key, title, status, at, source):
    return dict(repository=_text(repo), kind=kind, key=_text(key), title=_text(title),
                status=_text(status or 'UNKNOWN'), observed_at=_text(at or ''), source=source)


def _workflow(db, tables, home):
    if 'records' not in tables:
        return
    for (payload,) in db.execute("SELECT r.data FROM records r JOIN events e ON e.kind=r.kind AND e.entity_id=r.id AND e.revision=1 WHERE r.kind='shelf_history' ORDER BY e.seq"):
        yield json.loads(payload)
    params = ','.join('?' for _ in _KINDS)
    records = {(kind, key): (json.loads(data), status, at) for kind, key, data, status, at in
               db.execute(f'SELECT kind,id,data,status,updated_at FROM records '
                          f'WHERE kind IN ({params}) ORDER BY kind,id', _KINDS)}
    histories = defaultdict(list)
    versions = defaultdict(list)
    for (payload,) in db.execute("SELECT data FROM records WHERE kind='draft_version' ORDER BY updated_at,id"):
        version=json.loads(payload)
        if re.fullmatch('[0-9a-f]{64}',version.get('digest','')):
            versions[version['finding_id']].append(version['digest'])
    if 'events' in tables:
        for kind, key, at, status, reason, revision, payload_hash in db.execute(
                f'SELECT kind,entity_id,at,new_status,reason,revision,payload_hash FROM events '
                f'WHERE kind IN ({params}) ORDER BY seq', _KINDS):
            # Repository approval reasons can contain the operator's raw confirmation.
            if kind == 'repository' and status == 'APPROVED':
                reason = 'Repository approved'
            histories[kind, key].append(dict(at=_text(at), status=_text(status or 'UNKNOWN'),
                                             reason=_text(reason),revision=revision,digest=payload_hash))

    def data_for(kind, key):
        return records.get((kind, key), ({}, None, None))[0]

    for (kind, key), (data, status, at) in records.items():
        draft = data.get('draft', {}) if kind == 'draft' else {}
        submission = data_for('submission', data.get('submission_id')) if kind == 'feedback' else {}
        fid = data.get('finding_id') or draft.get('finding_id') or submission.get('finding_id') or key
        finding = data_for('finding', fid)
        repo = (data.get('repository') or draft.get('repository') or submission.get('repository')
                or finding.get('repository') or (key if kind == 'repository' else ''))
        if not _SLUG.fullmatch(repo):
            continue
        label, identity = kind, key
        title = data.get('title') or draft.get('title') or finding.get('title') or key
        material = url = None
        if kind == 'repository':
            url = f'https://github.com/{repo}'
        elif kind == 'submission':
            at = data.get('last_checked_at') or at
            label = str(data.get('submission_type')).lower()
            identity = str(data.get('number'))
            if label not in ('pr', 'issue') or not re.fullmatch('[1-9][0-9]*', identity):
                continue
            route = 'pull' if label == 'pr' else 'issues'
            url = f'https://github.com/{repo}/{route}/{identity}'
        elif kind == 'test_run':
            label, material = 'test', data.get('log_path')
            title = data.get('purpose') or title
        elif kind == 'draft':
            status = status or 'PREPARED'
            if re.fullmatch(r'[\w.-]+', key) and key not in ('.', '..'):
                material = home / 'reports' / 'submissions' / key / 'draft.md'
        elif kind == 'finding':
            material = data.get('last_patch') or data.get('file_path')
        elif kind in ('diff_review', 'validation'):
            label, identity = 'review', f'{kind}:{key}'
            status = data.get('result') or status
            if kind == 'diff_review':
                status = 'APPROVED' if data.get('approved') else 'NOT_APPROVED'
        elif kind == 'audit_run':
            label = 'scan'
        elif kind == 'feedback':
            title = data.get('type') or title
            status = 'INACTIVE' if data.get('active') is False else (status or 'ACTIVE')
            parts = urlsplit(data.get('url') or '')
            if parts.netloc == 'github.com' and parts.path.startswith(f'/{repo}/'):
                url = urlunsplit(('https', 'github.com', parts.path, '', parts.fragment))
        row = _row(repo, label, identity, title, status, at, 'workflow')
        if finding:
            row['finding_id'] = _text(fid)
        if material:
            row['material'] = _text(material)
            if kind == 'draft':
                row['versions'] = [_text(Path(material).parent/'versions'/h/'draft.json') for h in versions[key]]
        if url:
            row['url'] = _text(url)
        if (kind, key) in histories:
            row['events'] = histories[kind, key]
        yield row

    if {'review_rounds', 'review_receipts'} <= tables:
        for rid, subject, kind, role, phase, verdict, at in db.execute('''
                SELECT r.id,r.subject,r.kind,c.role,c.phase,
                       json_extract(c.envelope,'$.decision.verdict'),
                       json_extract(c.envelope,'$.decision.reviewed_at')
                FROM review_rounds r JOIN review_receipts c ON c.round_id=r.id
                ORDER BY r.seq,c.role,c.phase'''):
            repo = subject if kind == 'profile' else data_for('finding', subject).get('repository', '')
            if _SLUG.fullmatch(repo):
                yield _row(repo, 'review', f'{rid}:{role}:{phase}', f'{kind}: {role} {phase}',
                           verdict, at, 'workflow')


def _pilot(db, tables, home):
    if 'scans' in tables:
        for repo, sha, day, docs in db.execute('SELECT repo,sha,day,docs FROM scans ORDER BY repo,day,sha'):
            if _SLUG.fullmatch(repo):
                yield _row(repo, 'scan', sha, f'Scanned {docs} documents', 'COMPLETED', day, 'pilot')
    if {'scans','queue'} <= tables:
        for repo,key,sha,title,decision,current,day in db.execute('''SELECT q.repo,q.id,q.sha,q.title,q.decision,q.current,s.day
            FROM queue q LEFT JOIN scans s ON s.repo=q.repo AND s.sha=q.sha'''):
            if _SLUG.fullmatch(repo):
                yield _row(repo,'draft',f'pilot:{key}:{sha}',title,decision if current else 'STALE',day,'pilot')


def source_rows(home: Path, pilot: Path | None) -> Iterator[dict]:
    """Take one read transaction per existing database; release it before yielding rows."""
    inputs = [(home / 'data' / 'contribution.db', _workflow)]
    if pilot is not None:
        path = pilot / 'pilot.sqlite3' if pilot.is_dir() else pilot
        if not path.is_file(): raise ValueError('Pilot database not found: '+str(path))
        inputs.append((path, _pilot))
    rows = []
    for path, project in inputs:
        if path.is_file():
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                db.execute('BEGIN')
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if project is _pilot and 'scans' not in tables: raise ValueError('Not a pilot database: '+str(path))
                rows.extend(project(db, tables, home))
    yield from rows
