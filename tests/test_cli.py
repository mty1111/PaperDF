"""Offline headless entrypoint and durable CLI workflow regression tests."""
import configparser
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from PyPDF2 import PdfWriter
import paperdf_cli as cli
from paperdf_config import api_key_for, load_config
from paperdf_session import BatchStore


def response(title='Notes'):
    return {'authors': ['Ada Lovelace'], 'year': '1843', 'journal': '', 'title': title,
            'evidence': {name: [] for name in ('authors', 'year', 'journal', 'title')},
            'academic': {'author_details': [{'literal': 'Ada Lovelace', 'kind': 'person',
                                            'given': 'Ada', 'family': 'Lovelace', 'suffix': ''}],
                         'document_kind': 'preprint',
                         'dates': [{'kind': 'preprint', 'year': '1843', 'page': 1, 'quote': '1843'}]}}


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.root / 'state'
        self.pdf = self.make_pdf('乱码.pdf')
        self.sdk = Mock()
        self.sdk.models.generate_content.return_value.text = json.dumps(response())

    def make_pdf(self, name, width=100):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        writer = PdfWriter()
        writer.add_blank_page(width=width, height=100)
        with path.open('wb') as stream:
            writer.write(stream)
        return path

    def command(self, *args, sdk=None, stopped=None):
        options = cli.parser().parse_args(['--store-dir', str(self.store), *map(str, args)])
        out, err = io.StringIO(), io.StringIO()
        code = cli.execute(options, client_factory=lambda key: sdk or self.sdk,
                           stopped=stopped, out=out, err=err)
        return code, json.loads(out.getvalue()), err.getvalue()

    def saved(self):
        return BatchStore(self.store / 'cli-batch-state').load()

    def test_preview_then_apply_after_restart_and_undo_without_new_request(self):
        original = self.pdf.read_bytes()
        code, preview, progress = self.command('run', self.pdf, '--pattern', '{title}.pdf')
        self.assertEqual(code, 0)
        self.assertEqual(preview['rows'][0]['status'], 'Ready')
        self.assertTrue(self.pdf.exists())
        self.assertFalse((self.store / 'rename-history').exists())
        self.assertIn('Analyzed: 1 / 1', progress)
        saved = self.saved()
        self.assertTrue(Path(saved['rows'][0]['snippet_path']).exists())
        self.assertNotIn('api_key', saved['context'])
        code, applied, progress = self.command('continue', '--apply')
        self.assertEqual(code, 0)
        self.assertEqual(applied['summary']['renamed'], 1)
        self.assertIn('Renamed: 1 / 1', progress)
        self.assertEqual((self.root / 'Notes.pdf').read_bytes(), original)
        self.assertFalse(self.pdf.exists())
        self.assertEqual(self.sdk.models.generate_content.call_count, 1)
        code, undone, _ = self.command('undo')
        self.assertEqual(code, 0)
        self.assertEqual(undone['operations'][0]['status'], 'restored')
        self.assertEqual(undone['summary']['renamed'], 0)
        self.assertEqual(self.pdf.read_bytes(), original)

    def test_occupied_destination_is_held_and_never_auto_suffixed(self):
        destination = self.make_pdf('Notes.pdf', width=120)
        contents = destination.read_bytes()
        code, result, _ = self.command('run', self.pdf, '--pattern', '{title}.pdf', '--apply')
        self.assertEqual(code, 1)
        self.assertEqual(result['rows'][0]['status'], 'Needs review')
        self.assertTrue(self.pdf.exists())
        self.assertEqual(destination.read_bytes(), contents)
        self.assertEqual(len(list(self.root.glob('*.pdf'))), 2)

    def test_planned_duplicate_is_rechecked_when_earlier_source_changes(self):
        duplicate = self.root / 'copy.pdf'
        duplicate.write_bytes(self.pdf.read_bytes())
        code, result, _ = self.command('run', self.pdf, duplicate, '--pattern', '{title}.pdf')
        self.assertEqual(code, 0)
        self.assertEqual(result['rows'][1]['status'], 'Duplicate')
        self.make_pdf(self.pdf.name, width=200)
        self.sdk.models.generate_content.return_value.text = json.dumps(response('New notes'))
        code, result, _ = self.command('continue', '--apply')
        self.assertEqual(code, 0)
        self.assertEqual(result['summary']['renamed'], 2)
        self.assertTrue((self.root / 'Notes.pdf').exists())
        self.assertTrue((self.root / 'New notes.pdf').exists())

    def test_schema_failure_is_retryable_and_continue_finishes(self):
        self.sdk.models.generate_content.return_value.text = '{"title":"bad schema"}'
        code, result, _ = self.command('run', self.pdf, '--apply', '--pattern', '{title}.pdf')
        self.assertEqual(code, 1)
        self.assertEqual(result['rows'][0]['status'], 'Error')
        self.assertTrue(self.pdf.exists())
        self.sdk.models.generate_content.return_value.text = json.dumps(response())
        code, result, _ = self.command('continue', '--apply')
        self.assertEqual(code, 0)
        self.assertEqual(result['summary']['renamed'], 1)
        self.assertEqual(self.sdk.models.generate_content.call_count, 2)

    def test_cache_only_new_batch_works_without_credentials(self):
        self.command('run', self.pdf, '--apply', '--pattern', '{title}.pdf')
        with patch('paperdf_gemini.genai.Client') as create:
            code, result, _ = self.command('run', self.root / 'Notes.pdf', '--apply',
                                          '--pattern', '{title}.pdf', sdk=cli._LazyGeminiClient(''))
        self.assertEqual(code, 0)
        self.assertEqual(result['rows'][0]['extraction_source'], 'cache')
        create.assert_not_called()

    def test_changed_source_is_reextracted_before_applying_saved_plan(self):
        self.command('run', self.pdf, '--pattern', '{title}.pdf')
        self.make_pdf(self.pdf.name, width=200)
        self.sdk.models.generate_content.return_value.text = json.dumps(response('New notes'))
        code, result, _ = self.command('continue', '--apply')
        self.assertEqual(code, 0)
        self.assertTrue((self.root / 'New notes.pdf').exists())
        self.assertFalse((self.root / 'Notes.pdf').exists())
        self.assertEqual(self.sdk.models.generate_content.call_count, 2)

    def test_stop_checkpoints_and_keeps_unstarted_files_for_continue(self):
        second = self.make_pdf('second.pdf', width=110)
        stopped = threading.Event()
        def finish(*args, **kwargs):
            stopped.set()
            return Mock(text=json.dumps(response()))
        self.sdk.models.generate_content.side_effect = finish
        code, result, _ = self.command('run', self.pdf, second, '--apply', '--pattern', '{title}.pdf', stopped=stopped)
        self.assertEqual(code, 130)
        self.assertEqual(result['summary']['analyzed'], 1)
        self.assertEqual(result['summary']['renamed'], 0)
        self.assertTrue(self.pdf.exists())
        self.assertTrue(second.exists())
        self.assertEqual(self.saved()['rows'][1]['status'], 'Pending')
        self.sdk.models.generate_content.side_effect = None
        self.sdk.models.generate_content.return_value.text = json.dumps(response('Followup notes'))
        code, result, _ = self.command('continue', '--apply')
        self.assertEqual(code, 0)
        self.assertEqual(result['summary']['renamed'], 2)

    def test_force_refresh_persists_across_failed_request_and_restart(self):
        self.command('run', self.pdf, '--pattern', '{title}.pdf')
        self.sdk.models.generate_content.side_effect = TimeoutError('test timeout')
        code, result, _ = self.command('run', self.pdf, '--new-batch', '--force-refresh')
        self.assertEqual(code, 1)
        self.assertTrue(result['rows'][0]['force_refresh'])
        self.sdk.models.generate_content.side_effect = None
        self.sdk.models.generate_content.return_value.text = json.dumps(response('Refreshed'))
        code, result, _ = self.command('continue')
        self.assertEqual(code, 0)
        self.assertEqual(result['rows'][0]['metadata']['title'], 'Refreshed')
        self.assertEqual(self.sdk.models.generate_content.call_count, 3)

    def test_uncertain_dates_remain_unrenamed(self):
        data = response()
        data['academic']['dates'] = []
        self.sdk.models.generate_content.return_value.text = json.dumps(data)
        code, result, _ = self.command('run', self.pdf, '--apply')
        self.assertEqual(code, 1)
        self.assertTrue(self.pdf.exists())
        self.assertEqual(result['rows'][0]['status'], 'Needs review')

    def test_unfinished_batch_is_preserved_unless_explicitly_replaced(self):
        self.command('run', self.pdf)
        before = self.saved()
        with self.assertRaisesRegex(ValueError, 'Unfinished CLI'):
            self.command('run', self.pdf)
        self.assertEqual(before, self.saved())
        self.command('run', self.pdf, '--new-batch')
        self.assertNotEqual(before['batch_id'], self.saved()['batch_id'])

    def test_status_csv_and_cli_state_do_not_replace_desktop_batch(self):
        gui = BatchStore(self.store / 'batch-state')
        gui.start([str(self.pdf)], {'pages': 1, 'is_book': False,
                  'naming': {'pattern': '{title}.pdf', 'author_format': '{surname}', 'unpublished': 'Unknown'}})
        original = gui.path.read_bytes()
        self.command('run', self.pdf)
        report = self.root / 'results.csv'
        code, result, _ = self.command('status', '--csv', report)
        self.assertEqual(code, 0)
        self.assertEqual(result['summary']['total'], 1)
        self.assertIn('Ada Lovelace', report.read_text(encoding='utf-8-sig'))
        self.assertEqual(gui.path.read_bytes(), original)
        self.assertEqual(self.sdk.models.generate_content.call_count, 1)

    def test_recursive_collection_deduplicates_and_excludes_review_snapshots(self):
        nested = self.make_pdf('nested/paper.PDF')
        cache_pdf = self.make_pdf('state/cli-batch-state/abc/snapshot.pdf')
        self.assertEqual(cli.collect_paths([self.root], False, self.store), [str(self.pdf)])
        paths = cli.collect_paths([self.root, self.pdf], True, self.store)
        self.assertEqual(set(paths), {str(self.pdf), str(nested)})
        self.assertNotIn(str(cache_pdf), paths)

    def test_config_unicode_aliases_percent_and_key_precedence(self):
        self.store.mkdir()
        config = configparser.ConfigParser(interpolation=None)
        config['Settings'] = {'journal_aliases': 'XYZ = 中文 100% Journal', 'api_key': 'saved-test-key'}
        with (self.store / 'pdf_metadata_renamer.config').open('w', encoding='utf-8') as stream:
            config.write(stream)
        settings = load_config(self.store)['Settings']
        self.assertEqual(settings['journal_aliases'], 'XYZ = 中文 100% Journal')
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'env-test-key'}):
            self.assertEqual(api_key_for(self.store, settings), 'saved-test-key')
            self.assertEqual(api_key_for(self.store, {}), 'env-test-key')

    def test_no_tk_import_or_user_config_write_in_headless_subprocess(self):
        code = '''import importlib.abc, sys
class NoTk(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'tkinter' or fullname.startswith('tkinter.'):
            raise AssertionError('Headless entrypoint imported Tk')
sys.meta_path.insert(0, NoTk())
import paperdf_cli
raise SystemExit(paperdf_cli.main(['--store-dir', sys.argv[1], 'status']))
'''
        done = subprocess.run([sys.executable, '-c', code, str(self.store)], cwd=Path(__file__).parents[1],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)['summary']['total'], 0)
        self.assertFalse(self.store.exists())

    def test_invalid_page_argument_rejected_before_processing(self):
        with patch('sys.stderr', new=io.StringIO()), self.assertRaises(SystemExit) as error:
            cli.parser().parse_args(['run', str(self.pdf), '--pages', '51'])
        self.assertEqual(error.exception.code, 2)
        self.assertFalse(self.store.exists())


if __name__ == '__main__':
    unittest.main()
