"""One-way Canvas assignment sync. No assignment data is written to disk/logs."""
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse
import requests


class SyncError(Exception):
    pass


def text(value):
    return [{"text": {"content": str(value)[:2000]}}] if value else []


def plain(prop, key='rich_text'):
    return ''.join(x.get('plain_text', x.get('text', {}).get('content', '')) for x in prop.get(key, []))


def instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')) if value else None


def kind(a):
    title = a.get('name', '').lower()
    if re.search(r'\b(exam|midterm|final exam)\b', title):
        return 'Exam'
    if re.search(r'\bproject\b', title):
        return 'Project'
    if a.get('is_quiz_assignment') or re.search(r'\bquiz\b', title):
        return 'Quiz'
    return 'Assignment'


class Client:
    def __init__(self, base, token, notion=False):
        self.base = base.rstrip('/')
        self.session = requests.Session()
        self.session.headers['Authorization'] = 'Bearer ' + token
        if notion:
            self.session.headers['Notion-Version'] = '2025-09-03'
        self.notion = notion

    def request(self, method, path, **kwargs):
        url = path if path.startswith('https://') else self.base + path
        if urlparse(url).netloc != urlparse(self.base).netloc:
            raise SyncError('Unexpected pagination host')
        for attempt in range(4):
            if self.notion:
                time.sleep(0.35)
            try:
                response = self.session.request(method, url, timeout=30, allow_redirects=False, **kwargs)
            except requests.RequestException:
                raise SyncError('Connection failed; next scheduled run will retry') from None
            if response.status_code == 429 and attempt < 3:
                time.sleep(min(float(response.headers.get('Retry-After', '5')), 30))
                continue
            if not response.ok or response.is_redirect:
                label = 'Notion' if self.notion else 'Canvas'
                raise SyncError(f'{label} HTTP {response.status_code}')
            return response
        raise SyncError('Rate limit exceeded')

    def canvas_list(self, path, params=None):
        result = []
        while path:
            response = self.request('GET', path, params=params)
            result.extend(response.json())
            path = response.links.get('next', {}).get('url')
            params = None
        return result

    def pages(self, ds):
        result, cursor = [], None
        while True:
            body = {'page_size': 100}
            if cursor:
                body['start_cursor'] = cursor
            data = self.request('POST', f'/data_sources/{ds}/query', json=body).json()
            result.extend(data['results'])
            if not data.get('has_more'):
                return result
            cursor = data['next_cursor']


def changes(page, props):
    old = page.get('properties', {})
    result = {}
    for name, value in props.items():
        key = next(iter(value))
        previous = old.get(name, {})
        if key in ('title', 'rich_text'):
            same = plain(previous, key) == plain(value, key)
        elif key == 'date':
            before = instant((previous.get('date') or {}).get('start'))
            after = instant((value.get('date') or {}).get('start'))
            # Notion stores date property times at minute precision.
            same = (before.replace(second=0, microsecond=0) if before else None) == (after.replace(second=0, microsecond=0) if after else None)
        else:
            same = previous.get(key) == value[key]
        if not same:
            result[name] = value
    return result


def sync(canvas, notion, ds, term, now=None, dry_run=False):
    now = now or datetime.now(timezone.utc)
    pages = notion.pages(ds)
    by_key, by_url = {}, {}
    for page in pages:
        p = page['properties']
        key = plain(p.get('Canvas ID', {}))
        url = p.get('Canvas Link', {}).get('url')
        if key:
            if key in by_key:
                raise SyncError('Duplicate Canvas IDs already exist in Notion; manual review needed')
            by_key[key] = page
        if url:
            by_url[url] = page
    courses = canvas.canvas_list('/courses', {'enrollment_type': 'student', 'enrollment_state': 'active', 'per_page': 100})
    courses = [c for c in courses if str(c.get('enrollment_term_id')) == str(term)]
    if not courses:
        raise SyncError('No courses found for configured semester')
    counts = dict(courses=len(courses), assignments=0, created=0, updated=0, unchanged=0, skipped=0)
    for course in courses:
        assignments = canvas.canvas_list(f"/courses/{course['id']}/assignments", {'per_page':100, 'override_assignment_dates':'true', 'include[]':'submission'})
        for a in assignments:
            counts['assignments'] += 1
            if a.get('published') is False:
                counts['skipped'] += 1
                continue
            key = f"{course['id']}:{a['id']}"
            url = a.get('html_url')
            page = by_key.get(key) or by_url.get(url)
            due = a.get('due_at')
            # Start with upcoming/undated work; keep tracking existing rows after due date.
            submitted = (a.get('submission') or {}).get('workflow_state') in ('submitted', 'graded')
            if not page and ((due and instant(due) < now) or submitted):
                counts['skipped'] += 1
                continue
            props = {
                'Assignment Name': {'title': text(a['name'])},
                'Canvas ID': {'rich_text': text(key)},
                'Canvas Link': {'url': url},
                'Canvas Course': {'rich_text': text(course.get('course_code') or course['name'])},
                'Deadline': {'date': {'start': due} if due else None},
            }
            if page:
                delta = changes(page, props)
                if delta:
                    if not dry_run:
                        notion.request('PATCH', f"/pages/{page['id']}", json={'properties': delta})
                    counts['updated'] += 1
                else:
                    counts['unchanged'] += 1
            else:
                props['Type'] = {'select': {'name': kind(a)}}
                # Done, Courses relation, and manual Type edits are never overwritten.
                if not dry_run:
                    page = notion.request('POST', '/pages', json={'parent': {'type':'data_source_id', 'data_source_id':ds}, 'properties':props}).json()
                    by_key[key] = page
                    if url:
                        by_url[url] = page
                counts['created'] += 1
    return counts


def main():
    try:
        canvas = Client('https://bcourses.berkeley.edu/api/v1', os.environ['CANVAS_TOKEN'])
        notion = Client('https://api.notion.com/v1', os.environ['NOTION_TOKEN'], notion=True)
        ds = os.environ['ASSIGNMENTS_DATA_SOURCE_ID']
        term = os.environ['CANVAS_TERM_ID']
        dry = '--dry-run' in sys.argv
        print(('Preview: ' if dry else 'Sync: ') + str(sync(canvas, notion, ds, term, dry_run=dry)))
        if '--verify-replay' in sys.argv and not dry:
            replay = sync(canvas, notion, ds, term)
            if replay['created'] or replay['updated']:
                raise SyncError('Replay changed rows; inspect source changes before assuming idempotence')
            print('Replay passed: no duplicates or unnecessary updates.')
    except (SyncError, KeyError, ValueError) as exc:
        print(str(exc) if isinstance(exc, SyncError) else 'Invalid configuration or response; sync stopped safely.')
        sys.exit(1)


if __name__ == '__main__':
    main()
