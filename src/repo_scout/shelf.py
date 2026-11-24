"""Repository-centric views of local observations. No GitHub or model calls."""
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
import html
import json
from pathlib import Path
import re

from .common import atomic_write, digest, redact
from .shelf_sources import source_rows


def slug(value):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+',value) or value.split('/')[1] in ('.','..'):
        raise ValueError('Invalid GitHub repository')
    return value.lower()


def add_parser(commands):
    group = commands.add_parser('shelf', help='按仓库浏览本地历史，无网络调用')
    group.add_argument('--pilot', type=Path, help='指定一个 pilot 数据库或目录')
    subs = group.add_subparsers(dest='shelf_action', required=True)
    subs.add_parser('list')
    subs.add_parser('show').add_argument('repository')
    subs.add_parser('search').add_argument('query')
    subs.add_parser('export')
    subs.add_parser('import').add_argument('file', type=Path)


def stamp(value):
    if not value:
        return ''
    if len(value) == 10:
        return date.fromisoformat(value).isoformat()
    dt = datetime.fromisoformat(value.replace('Z','+00:00'))
    if dt.tzinfo is None:
        raise ValueError('observed_at needs a date or a timestamp with timezone')
    return dt.astimezone(timezone.utc).isoformat()


def manual(value):
    fields = {'repository','kind','key','title','status','observed_at','url','material'}
    if not isinstance(value,dict) or set(value)-fields or not all(isinstance(v,str) for v in value.values()):
        raise ValueError('Invalid shelf observation fields')
    row = dict(value, repository=slug(value['repository']), observed_at=stamp(value['observed_at']), source='manual')
    kind, key = row['kind'], row['key']
    if not row['title'].strip() or not row['status'].strip() or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,159}',key):
        raise ValueError('Invalid shelf title, status or key')
    if kind in ('issue','pr','comment'):
        route = {'pr':'pull','issue':'issues','comment':'(?:issues|pull)'}[kind]
        pat = rf'https://github\.com/{re.escape(row["repository"])}/{route}/([1-9][0-9]*)'
        if kind=='comment': pat += r'#issuecomment-([1-9][0-9]*)'
        match = re.fullmatch(pat,row.get('url',''),re.I)
        if not match or key != match.groups()[-1]:
            raise ValueError('URL must match repository, activity type and remote ID')
    elif kind not in ('finding','draft','scan','review','test') or 'url' in row:
        raise ValueError('Invalid local activity type or URL')
    from .bundle import secret_reason
    text=json.dumps(row,ensure_ascii=False)
    if secret_reason(text) or redact(text)!=text:
        raise ValueError('Possible credential in shelf observation')
    return row


def import_history(home, path):
    from .database import Store
    values=json.loads(path.read_text('utf8'))
    if not isinstance(values,list): raise ValueError('Expected an array of observations')
    rows=[manual(v) for v in values]
    added=0
    with Store(home/'data'/'contribution.db') as store, store.atomic():
        for row in rows:
            key=digest(row)
            if store.get('shelf_history',key) is None:
                store.put('shelf_history',key,row,reason='local historical observation',expected_version=0)
                added+=1
    return {'added':added,'unchanged':len(rows)-added}


def books(home,pilot=None):
    grouped=defaultdict(lambda: defaultdict(list))
    for row in source_rows(home,pilot):
        try: repo=slug(row['repository'])
        except ValueError: continue
        try: row['observed_at']=stamp(row.get('observed_at',''))
        except (TypeError,ValueError,OverflowError): row['observed_at']=''
        grouped[repo][row['kind'],row['key']].append(row)
    result={}
    for repo,groups in sorted(grouped.items()):
        entries=[]
        for observations in groups.values():
            observations.sort(key=lambda r:(r['observed_at'],r['source']=='workflow'))
            entries.append({**observations[-1],'observations':observations})
        entries.sort(key=lambda r:(stamp(r.get('observed_at','')),r['kind'],r['key']),reverse=True)
        result[repo]=entries
    return result


def esc(value):
    return re.sub(r'([\\`*_{}\[\]()#!|])',r'\\\1',html.escape(' '.join(str(value).split())))


def page(repo,entries):
    lines=[f'# {esc(repo)}','', '按本地观察时间排列。上游状态不是实时查询；发布不代表已采纳。','']
    for row in entries:
        lines.extend([f"## {esc(row['kind'])} · {esc(row['title'])}", f"编号：{esc(row['key'])}",''])
        for obs in row['observations']:
            lines.append(f"- {esc(obs['observed_at'] or '时间未知')} · {esc(obs['source'])} · {esc(obs['status'])}")
            if obs.get('url'): lines.append(f"  - 上游：{esc(obs['url'])}")
            if obs.get('material'): lines.append(f"  - 材料：{esc(obs['material'])}")
            for version in obs.get('versions',[]): lines.append(f'  - 草稿版本：{esc(version)}')
            for ev in obs.get('events',[]):
                lines.append(f"  - {esc(ev['at'])} · {esc(ev['status'])} · {esc(ev['reason'])}")
        lines.append('')
    return '\n'.join(lines)+'\n'


def run(args,home):
    if args.shelf_action=='import': return import_history(home,args.file)
    data=books(home,args.pilot)
    if args.shelf_action=='show':
        repo=slug(args.repository)
        if repo not in data: raise ValueError('No shelf records for '+repo)
        return {'repository':repo,'entries':data[repo]}
    if args.shelf_action=='search':
        q=args.query.casefold().strip()
        if not q: raise ValueError('Empty shelf search')
        return [r for rs in data.values() for r in rs if q in json.dumps(r,ensure_ascii=False).casefold()]
    summary=[{'repository':name,'counts':dict(Counter(r['kind'] for r in rs)),
              'follow_up':sum(r['kind'] in ('issue','pr') and r['status'] in ('OPEN','SUBMITTED','UNDER_REVIEW','CHANGES_REQUESTED') for r in rs),
              'last_observed_at':rs[0]['observed_at']} for name,rs in data.items()]
    if args.shelf_action=='list': return summary
    dest=home/'shelf'; index=['# 仓库书架','']
    for repo,entries in data.items():
        owner,name=repo.split('/')
        # Prefix each component to avoid Windows reserved filenames. Reject
        # filesystem redirects before writing a projection outside this home.
        path=dest/('owner-'+owner)/('repo-'+name+'.md')
        if not path.resolve().is_relative_to(dest.absolute()) or path.parent.resolve()!=path.parent.absolute():
            raise ValueError('Unsafe shelf export directory')
        atomic_write(path,page(repo,entries)); index.append(f'- [{esc(repo)}]({path.relative_to(dest).as_posix()})')
    if dest.resolve()!=dest.absolute(): raise ValueError('Unsafe shelf export directory')
    atomic_write(dest/'index.md','\n'.join(index)+'\n')
    return {'repositories':len(data),'index':str(dest/'index.md')}
