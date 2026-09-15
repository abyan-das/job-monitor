import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from job_monitor import Job, Monitor, run_state_file, sync_local_notion
from notion_sync import NotionSync, job_id

ENV = {'NOTION_TOKEN': 'test-only', 'NOTION_DATA_SOURCE_ID': 'test-source'}
JOB = Job('Example', '123', 'Associate Product Manager', 'NY', 'https://example.com/jobs/123')


class NotionTests(unittest.TestCase):
    @patch.dict('os.environ', ENV)
    def test_insert_and_replay_preserve_application_state(self):
        client = NotionSync()
        client.request = Mock(side_effect=[{'results': []}, {'id': 'page'}, {'results': [{'id': 'page'}]}])
        self.assertEqual(client.sync(JOB, '2026-09-15T12:00:00Z'), 'page')
        self.assertEqual(client.sync(JOB, '2026-09-15T12:00:00Z'), 'page')
        calls = client.request.call_args_list
        self.assertEqual([x.args[0] for x in calls], ['data_sources/test-source/query', 'pages', 'data_sources/test-source/query'])
        props = calls[1].args[1]['properties']
        self.assertEqual(props['Status']['select']['name'], 'New')
        self.assertNotIn('Deadline', props)
        self.assertEqual(props['Role Link']['url'], JOB.url)

    def test_job_identity_ignores_title_edits(self):
        self.assertEqual(job_id(JOB), job_id(Job('Example', '123', 'Updated role title', '', JOB.url)))

    @patch.dict('os.environ', ENV)
    def test_no_automatic_retry_on_ambiguous_create(self):
        client = NotionSync()
        client.session = Mock()
        client.session.post.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            client.request('pages', {})
        self.assertEqual(client.session.post.call_count, 1)

    def setup_state(self, root):
        monitor = Mock()
        monitor.sources.return_value = [{'name': 'Example'}]
        monitor.fetch.return_value = [JOB]
        monitor.matches.return_value = True
        return monitor, Path(root) / 'state.json'

    @patch.dict('os.environ', ENV)
    @patch('job_monitor.send_discord')
    @patch('job_monitor.notion_sync.NotionSync')
    def test_notion_failure_does_not_repeat_discord_and_retries_after_delisting(self, factory, discord):
        client = factory.return_value
        client.sync.side_effect = [RuntimeError('unavailable'), 'page']
        with tempfile.TemporaryDirectory() as root:
            monitor, path = self.setup_state(root)
            run_state_file(monitor, path)
            self.assertEqual(len(json.loads(path.read_text())['notion_pending']), 1)
            monitor.fetch.return_value = []
            run_state_file(monitor, path)
            self.assertEqual(json.loads(path.read_text())['notion_pending'], {})
            self.assertEqual(discord.call_count, 1)
            self.assertEqual(client.sync.call_count, 2)

    @patch.dict('os.environ', ENV)
    @patch('job_monitor.send_discord', side_effect=[RuntimeError('unavailable'), None])
    @patch('job_monitor.notion_sync.NotionSync')
    def test_discord_failure_does_not_block_notion(self, factory, discord):
        with tempfile.TemporaryDirectory() as root:
            monitor, path = self.setup_state(root)
            run_state_file(monitor, path)
            run_state_file(monitor, path)
            factory.return_value.sync.assert_called_once()
            self.assertEqual(discord.call_count, 2)
            self.assertEqual(json.loads(path.read_text())['discord_pending'], {})

    @patch.dict('os.environ', ENV)
    @patch('job_monitor.send_discord')
    @patch('job_monitor.notion_sync.NotionSync')
    def test_existing_state_and_bootstrap_do_not_import_old_jobs(self, factory, discord):
        with tempfile.TemporaryDirectory() as root:
            monitor, path = self.setup_state(root)
            path.write_text(json.dumps({'version': 1, 'seen': {JOB.key: {}}}))
            run_state_file(monitor, path)
            factory.return_value.sync.assert_not_called()
            path.unlink()
            run_state_file(monitor, path, bootstrap=True)
            factory.return_value.sync.assert_not_called()
            discord.assert_not_called()

    @patch.dict('os.environ', {'NOTION_TOKEN': '', 'NOTION_DATA_SOURCE_ID': ''})
    @patch('job_monitor.send_discord')
    def test_unconfigured_notion_keeps_pending_for_later(self, discord):
        with tempfile.TemporaryDirectory() as root:
            monitor, path = self.setup_state(root)
            run_state_file(monitor, path)
            self.assertIn(JOB.key, json.loads(path.read_text())['notion_pending'])

    @patch.dict('os.environ', ENV)
    @patch('job_monitor.notion_sync.NotionSync')
    def test_local_queue_retries_independently(self, factory):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / 'config.json'
            config.write_text(json.dumps({'companies': [{'name': 'Example', 'provider': 'generic'}]}))
            db = Path(root) / 'jobs.sqlite3'
            monitor = Monitor(config, db)
            monitor.fetch = Mock(return_value=[JOB])
            monitor.matches = Mock(return_value=True)
            monitor.run()
            factory.return_value.sync.side_effect = [RuntimeError(), 'page']
            sync_local_notion(db)
            self.assertEqual(monitor.db.execute('select notion_synced from jobs').fetchone()[0], 0)
            sync_local_notion(db)
            self.assertEqual(monitor.db.execute('select notion_synced from jobs').fetchone()[0], 1)
            monitor.close()

if __name__ == '__main__':
    unittest.main()
