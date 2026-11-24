import json
import tempfile
import unittest
from pathlib import Path

from repo_scout.cli import parser
from repo_scout.database import Store
from repo_scout.shelf import import_history, run


class ShelfTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(); self.addCleanup(t.cleanup)
        self.home = Path(t.name)/'home'
        self.input = Path(__file__).resolve().parents[1]/'examples/shelf-history.json'

    def cmd(self,*args):
        return run(parser().parse_args(['shelf',*args]),self.home)

    def test_history_dedup_search_and_export(self):
        self.assertEqual(self.cmd('list'),[])
        self.assertFalse(self.home.exists())
        self.assertEqual(import_history(self.home,self.input)['added'],6)
        self.assertEqual(import_history(self.home,self.input)['added'],0)
        self.assertEqual(len(self.cmd('search','q96')),1)
        with Store(self.home/'data/contribution.db') as s:
            s.put('submission','sub',{'repository':'InfiniteField/HyperSDK','submission_type':'PR','number':112,'status':'MERGED','last_checked_at':'2027-01-01T12:00:00Z'})
        pr, = (r for r in self.cmd('show','infinitefield/hypersdk')['entries'] if r['kind']=='pr')
        self.assertEqual(pr['status'],'MERGED'); self.assertEqual(len(pr['observations']),2)
        self.assertEqual(self.cmd('show','sphinx-doc/sphinx')['entries'][0]['kind'],'comment')
        index=Path(self.cmd('export')['index']); self.assertTrue(index.is_file())
        page=(index.parent/'owner-infinitefield/repo-hypersdk.md').read_text('utf8')
        self.assertIn('MERGED',page); self.assertIn('DEFERRED',page)

    def test_mixed_import_is_all_or_nothing(self):
        rows=json.loads(self.input.read_text('utf8')); rows[-1]['repository']='other/project'
        f=self.home.parent/'bad.json'; f.write_text(json.dumps(rows),'utf8')
        with self.assertRaises(ValueError): import_history(self.home,f)
        self.assertFalse(self.home.exists())

    def test_same_day_manual_observations_follow_insertion_order(self):
        row=json.loads(self.input.read_text('utf8'))[0]
        f=self.home.parent/'same.json'; f.write_text(json.dumps([row,{**row,'status':'CLOSED'}]),'utf8')
        import_history(self.home,f); import_history(self.home,f)
        item,=self.cmd('show',row['repository'])['entries']
        self.assertEqual(item['status'],'CLOSED'); self.assertEqual(len(item['observations']),2)

    def test_comment_route_cannot_change_owner_and_old_overflow_date_is_unknown(self):
        from repo_scout.shelf import manual
        row={'repository':'issues/project','kind':'comment','key':'7','title':'t','status':'POSTED',
             'observed_at':'2026-01-01','url':'https://github.com/pull/project/issues/1#issuecomment-7'}
        with self.assertRaises(ValueError): manual(row)
        with Store(self.home/'data/contribution.db') as s:
            s.put('submission','sub',{'repository':'org/.github','submission_type':'PR','number':1,'last_checked_at':'0001-01-01T00:00:00+01:00'})
        self.assertEqual(self.cmd('show','org/.github')['entries'][0]['observed_at'],'')
