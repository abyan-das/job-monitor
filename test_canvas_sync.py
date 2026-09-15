import copy
import unittest
from datetime import datetime, timezone
from canvas_sync import sync, changes, kind, SyncError

NOW = datetime(2026, 9, 15, tzinfo=timezone.utc)
class Response:
    def __init__(self, body): self.body = body
    def json(self): return self.body
class Canvas:
    def __init__(self, items): self.items = items
    def canvas_list(self, path, params):
        if path == '/courses':
            return [{'id':1,'name':'Course','course_code':'TEST','enrollment_term_id':5751}, {'id':2,'enrollment_term_id':1}]
        assert path == '/courses/1/assignments'
        return self.items
class Notion:
    def __init__(self): self.rows = []; self.writes = []
    def pages(self, ds): return copy.deepcopy(self.rows)
    def request(self, method, path, json):
        self.writes.append((method, copy.deepcopy(json)))
        if method == 'POST':
            page = {'id':str(len(self.rows)+1),'properties':copy.deepcopy(json['properties'])}
            self.rows.append(page)
            return Response(copy.deepcopy(page))
        page = next(p for p in self.rows if path.endswith('/'+p['id']))
        page['properties'].update(copy.deepcopy(json['properties']))
        return Response(copy.deepcopy(page))
def assignment(**kw):
    return dict(id=7,name='Homework',html_url='https://example.org/7',due_at='2026-10-01T07:00:00Z',**kw)
class SyncTests(unittest.TestCase):
    def test_insert_replay_and_manual_completion(self):
        c,n = Canvas([assignment()]),Notion()
        self.assertEqual(sync(c,n,'ds',5751,NOW)['created'],1)
        n.rows[0]['properties']['Done'] = {'checkbox':True}
        n.rows[0]['properties']['Type'] = {'select':{'name':'Project'}}
        self.assertEqual(sync(c,n,'ds',5751,NOW)['unchanged'],1)
        c.items[0]['due_at']='2026-10-03T07:00:00Z'
        self.assertEqual(sync(c,n,'ds',5751,NOW)['updated'],1)
        self.assertEqual(set(n.writes[-1][1]['properties']),{'Deadline'})
        self.assertTrue(n.rows[0]['properties']['Done']['checkbox'])
    def test_removed_due_date(self):
        c,n=Canvas([assignment()]),Notion(); sync(c,n,'ds',5751,NOW)
        c.items[0]['due_at']=None
        self.assertEqual(sync(c,n,'ds',5751,NOW)['updated'],1)
        self.assertIsNone(n.rows[0]['properties']['Deadline']['date'])
    def test_past_submitted_unpublished_skip_and_undated_import(self):
        a=assignment(); a['due_at']='2026-01-01T00:00:00Z'
        b=assignment(submission={'workflow_state':'submitted'}); b['id']=8
        d=assignment(published=False); d['id']=9
        e=assignment(); e.update(id=10,due_at=None,html_url='https://example.org/10')
        n=Notion(); r=sync(Canvas([a,b,d,e]),n,'ds',5751,NOW)
        self.assertEqual((r['skipped'],r['created']),(3,1))
    def test_preview_has_no_writes(self):
        n=Notion(); r=sync(Canvas([assignment()]),n,'ds',5751,NOW,dry_run=True)
        self.assertEqual(r['created'],1); self.assertEqual(n.writes,[])
    def test_duplicate_ids_stop(self):
        c,n=Canvas([assignment()]),Notion(); sync(c,n,'ds',5751,NOW)
        n.rows.append(copy.deepcopy(n.rows[0]))
        with self.assertRaises(SyncError): sync(c,n,'ds',5751,NOW)
    def test_date_equivalence(self):
        page={'properties':{'Deadline':{'date':{'start':'2026-10-01T00:00:00-07:00'}}}}
        self.assertEqual(changes(page,{'Deadline':{'date':{'start':'2026-10-01T07:00:00Z'}}}),{})
    def test_type(self):
        self.assertEqual(kind({'name':'Midterm 1','is_quiz_assignment':True}),'Exam')
        self.assertEqual(kind({'name':'Project 1'}),'Project')
if __name__ == '__main__': unittest.main()
