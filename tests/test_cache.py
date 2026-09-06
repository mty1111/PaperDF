"""Cross-batch reuse is content/settings based, persistent, and fail-closed."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import pdf_metadata_renamer as app
from paperdf_cache import ExtractionCache
from paperdf_processing import can_retry_extraction
from paperdf_session import BatchStore
from paperdf_schema import FIELDS


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = self.folder / 'garbled.pdf'
        self.write_pdf()
        self.cache = ExtractionCache(self.folder / 'extraction-cache.sqlite3')
        self.sdk = Mock()
        self.sdk.files.upload.return_value.name = 'files/offline'
        self.payload = {'authors': ['Ada Lovelace'], 'year': '1843', 'journal': '', 'title': 'Notes',
                        'evidence': {field: [] for field in FIELDS}}
        self.payload['academic'] = {'document_kind': 'published_article',
            'author_details': [{'literal': 'Ada Lovelace', 'kind': 'person', 'given': 'Ada', 'family': 'Lovelace', 'suffix': ''}],
            'dates': [{'kind': 'publication', 'year': '1843', 'page': 1, 'quote': '1843'}]}
        self.sdk.models.generate_content.return_value.text = json.dumps(self.payload)

    def write_pdf(self, width=100):
        writer = app.PdfWriter()
        for _ in range(6):
            writer.add_blank_page(width=width, height=200)
        with self.source.open('wb') as stream:
            writer.write(stream)

    def extract(self, pages=4, book=False, model='fake', force=False, sdk=None, cache=None):
        return app.prepare_preview([str(self.source)], pages, book, sdk or self.sdk, model,
                                   lambda: False, lambda *args: None, self.folder / 'review',
                                   cache=cache or self.cache, force=force)[0]

    def test_renamed_copy_reuses_exact_snapshot_without_credentials_after_restart(self):
        first = self.extract()
        snippet = Path(first['snippet_path']).read_bytes()
        self.source = self.source.rename(self.folder / 'already renamed.pdf')
        with patch.object(app.genai, 'Client') as create:
            restored = self.extract(sdk=app._LazyGeminiClient(''), cache=ExtractionCache(self.cache.path))
        create.assert_not_called()
        self.assertEqual(restored['extraction_source'], 'cache')
        self.assertEqual(restored['metadata'], first['metadata'])
        self.assertEqual(Path(restored['snippet_path']).read_bytes(), snippet)
        self.assertEqual(restored['original_source'], str(self.source))
        self.assertEqual(self.sdk.files.upload.call_count, 1)
        with self.cache.connection() as db:
            self.assertEqual(db.execute('SELECT outcome FROM attempts ORDER BY id').fetchall(),
                             [('extracted',), ('cache_hit',)])

    def test_changed_content_pages_mode_model_and_rules_do_not_reuse(self):
        self.extract()
        for options in ({'pages': 5}, {'book': True}, {'model': 'different'}):
            self.assertEqual(self.extract(**options)['extraction_source'], 'model')
        context = self.cache.context(app.fingerprint_file(self.source), 4, False, 'fake', rules='future-v2')
        self.assertIsNone(self.cache.get(context))
        self.write_pdf(width=150)
        self.assertEqual(self.extract()['extraction_source'], 'model')
        self.assertEqual(self.sdk.files.upload.call_count, 5)

    def test_force_refresh_updates_cache_but_manual_corrections_do_not(self):
        first = self.extract()
        first['metadata']['title'] = 'Manual correction'
        self.assertEqual(self.extract()['metadata']['title'], 'Notes')
        self.sdk.models.generate_content.return_value.text = json.dumps(dict(self.payload, title='New title'))
        refreshed = self.extract(force=True)
        self.assertEqual(refreshed['metadata']['title'], 'New title')
        self.assertEqual(self.extract()['metadata']['title'], 'New title')
        self.assertEqual(self.sdk.files.upload.call_count, 2)

    def test_invalid_response_is_retryable_and_never_cached(self):
        self.sdk.models.generate_content.return_value.text = '{"title": ["bad shape"]}'
        failed = self.extract()
        self.assertTrue(can_retry_extraction(failed))
        with self.cache.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM results').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT outcome, error_type FROM attempts').fetchall(), [('failed', 'ValueError')])
        self.sdk.models.generate_content.return_value.text = json.dumps(self.payload)
        self.assertEqual(self.extract()['status'], 'Extracted')
        self.assertEqual(self.sdk.files.upload.call_count, 2)

    def test_empty_fields_stay_reviewable_on_cache_hit(self):
        self.sdk.models.generate_content.return_value.text = json.dumps(dict(self.payload, authors=[], year='', title='', academic={'document_kind': 'unknown', 'dates': [], 'author_details': []}))
        self.assertEqual(self.extract()['status'], 'Needs review')
        again = self.extract()
        self.assertEqual(again['status'], 'Needs review')
        self.assertFalse(again['selected'])
        self.assertEqual(self.sdk.files.upload.call_count, 1)

    def test_source_changed_during_request_is_not_cached(self):
        def generate(**kwargs):
            self.write_pdf(width=180)
            return Mock(text=json.dumps(self.payload))
        self.sdk.models.generate_content.side_effect = generate
        self.assertEqual(self.extract()['status'], 'Error')
        with self.cache.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM results').fetchone()[0], 0)

    def test_new_batch_cleanup_keeps_long_term_cache(self):
        store = BatchStore(self.folder / 'batch-state')
        context = {'pages': 4, 'is_book': False, 'naming': {'pattern': '{title}.pdf',
                   'author_format': '{surname}', 'unpublished': 'Unknown'}}
        rows = store.start([str(self.source)], context)
        rows[0].update(self.extract())
        store.save()
        store.start([str(self.source)], context)
        self.assertEqual(self.extract()['extraction_source'], 'cache')

    def test_force_policy_survives_failed_extraction_and_saved_batch(self):
        self.extract()
        store = BatchStore(self.folder / 'batch-state')
        rows = store.start([str(self.source)], {'pages': 4, 'is_book': False, 'force_refresh': True,
            'naming': {'pattern': '{title}.pdf', 'author_format': '{surname}', 'unpublished': 'Unknown'}})
        self.sdk.models.generate_content.side_effect = TimeoutError('offline timeout')
        app.extract_rows(rows, 4, False, self.sdk, 'fake', lambda: False, lambda *args: store.save(), cache=self.cache)
        restored = BatchStore(store.directory).load()['rows']
        self.assertTrue(restored[0]['force_refresh'])
        self.sdk.models.generate_content.side_effect = None
        self.sdk.models.generate_content.return_value.text = json.dumps(dict(self.payload, title='Fresh'))
        app.extract_rows(restored, 4, False, self.sdk, 'fake', lambda: False, lambda *args: None, cache=self.cache)
        self.assertEqual(restored[0]['metadata']['title'], 'Fresh')
        self.assertNotIn('force_refresh', restored[0])
        self.assertEqual(self.sdk.files.upload.call_count, 3)

    def test_damaged_cache_is_reported_without_silent_provider_request(self):
        self.extract()
        with self.cache.connection() as db:
            db.execute("UPDATE results SET snippet=X'00'")
        failed = self.extract()
        self.assertEqual(failed['status'], 'Error')
        self.assertIn('cache is damaged', failed['message'])
        self.assertEqual(self.sdk.files.upload.call_count, 1)

    def test_failed_transaction_retains_old_entry(self):
        self.extract()
        with self.cache.connection() as db:
            db.execute("CREATE TRIGGER reject_update BEFORE INSERT ON results BEGIN SELECT RAISE(ABORT, 'disk fault'); END")
        self.sdk.models.generate_content.return_value.text = json.dumps(dict(self.payload, title='New'))
        self.assertEqual(self.extract(force=True)['status'], 'Error')
        self.assertEqual(self.extract()['metadata']['title'], 'Notes')

    def test_clear_cache_removes_results_and_history(self):
        self.extract()
        self.cache.clear()
        with self.cache.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM results').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 0)
        self.assertEqual(self.extract()['extraction_source'], 'model')

    def test_mixed_batch_without_key_keeps_cache_hits_and_retryable_misses(self):
        self.extract()
        cached_path = self.source
        self.source = self.folder / 'new.pdf'
        self.write_pdf(width=130)
        with patch.object(app.genai, 'Client') as create:
            rows = app.prepare_preview([str(self.source), str(cached_path)], 4, False,
                                       app._LazyGeminiClient(''), 'fake', lambda: False,
                                       lambda *args: None, cache=self.cache)
        create.assert_not_called()
        self.assertTrue(can_retry_extraction(rows[0]))
        self.assertFalse(rows[0]['analysis_attempted'])
        self.assertEqual(rows[1]['extraction_source'], 'cache')
        self.assertEqual(rows[1]['status'], 'Extracted')

    def test_empty_pdf_does_not_make_a_provider_request(self):
        with self.source.open('wb') as stream:
            app.PdfWriter().write(stream)
        row = self.extract()
        self.assertTrue(can_retry_extraction(row))
        self.assertIn('no readable pages', row['message'])
        self.sdk.files.upload.assert_not_called()


if __name__ == '__main__':
    unittest.main()
