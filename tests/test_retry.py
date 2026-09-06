"""Offline checks that retry touches only failed extraction results."""
import copy
import io
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch


_config = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {'LOCALAPPDATA': _config.name, 'GEMINI_API_KEY': ''}):
    import pdf_metadata_renamer as app
from paperdf_processing import can_retry_extraction


def make_pdf(page_count):
    writer = app.PdfWriter()
    for index in range(page_count):
        writer.add_blank_page(width=100 + index, height=200)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


class RetryExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.review_dir = self.directory / 'review'
        self.metadata = {
            'authors': ['Ada Lovelace'], 'year': '1843', 'journal': '',
            'title': 'Notes', 'evidence': {},
        }

    def failure(self, name='Failed.PDF', pages=4):
        source = self.directory / name
        source.write_bytes(make_pdf(pages))
        return {
            'source': str(source), 'original_source': str(source),
            'metadata': {}, 'selected': False, 'needs_review': True,
            'fingerprint': app.fingerprint_file(source), 'status': 'Error',
            'extraction_error': 'Previous timeout', 'message': 'Previous timeout',
            'reason': 'Extraction failed: Previous timeout',
            'snippet_path': '', 'page_count': 0, 'is_book': False,
        }

    def retry(self, rows, *, error=None, metadata=None, cancelled=None, on_progress=None,
              pages=4, is_book=False):
        progress = []
        callback = on_progress or (lambda index, total, row: progress.append((index, total, row)))
        with patch.object(app, 'get_metadata_from_snippet',
                          return_value=self.metadata if metadata is None else metadata,
                          side_effect=error) as extract:
            retried = app.retry_failed_extractions(
                rows, pages, is_book, object(), 'retry-model',
                cancelled or (lambda: False), callback, review_dir=self.review_dir,
            )
        return retried, extract, progress

    def test_completed_and_attention_results_are_not_extraction_retries(self):
        rows = []
        for index, status in enumerate(('Renamed', 'Undone', 'Unchanged', 'Duplicate',
                                        'Needs review', 'Failed', 'Stopped')):
            row = self.failure(f'{index}.pdf')
            # A stale error string must not override the result's current state.
            row.update(status=status, metadata=copy.deepcopy(self.metadata))
            rows.append(row)
        rows.append({'source': str(self.directory / 'NoMarker.pdf'), 'status': 'Error'})
        before = copy.deepcopy(rows)
        retried, extract, progress = self.retry(rows)
        self.assertEqual(retried, [])
        self.assertEqual(rows, before)
        self.assertEqual(progress, [])
        extract.assert_not_called()
        self.assertTrue(all(not can_retry_extraction(row) for row in rows))

    def test_success_replaces_only_failure_in_place_and_clears_previous_error(self):
        completed = self.failure('Done.pdf')
        completed.update(status='Renamed', metadata=copy.deepcopy(self.metadata), needs_review=False)
        completed_before = copy.deepcopy(completed)
        failed = self.failure()
        rows = [completed, failed]
        self.assertTrue(can_retry_extraction(failed))
        retried, extract, progress = self.retry(rows)
        self.assertEqual(len(retried), 1)
        self.assertIs(retried[0], failed)
        self.assertIs(rows[1], failed)
        self.assertEqual(completed, completed_before)
        self.assertEqual(failed['metadata'], self.metadata)
        self.assertEqual(failed['status'], 'Extracted')
        self.assertTrue(failed['selected'])
        self.assertFalse(failed['needs_review'])
        self.assertNotIn('extraction_error', failed)
        self.assertNotIn('Previous timeout', failed.get('message', ''))
        self.assertNotIn('Previous timeout', failed.get('reason', ''))
        self.assertFalse(can_retry_extraction(failed))
        self.assertEqual(extract.call_count, 1)
        self.assertEqual(progress, [(1, 1, failed)])
        self.assertEqual(Path(failed['snippet_path']).read_bytes(), extract.call_args.args[0])

    def test_retry_recreates_snapshot_and_fingerprint_for_current_source(self):
        row = self.failure('CurrentCase.PDF', pages=6)
        original_source = str(self.directory / 'Original.PDF')
        row['original_source'] = original_source
        previous_fingerprint = row['fingerprint']
        Path(row['source']).write_bytes(make_pdf(2))
        retried, extract, _ = self.retry([row], pages=5, is_book=True)
        self.assertEqual(retried, [row])
        self.assertEqual(row['original_source'], original_source)
        self.assertEqual(Path(row['source']).name, 'CurrentCase.PDF')
        self.assertNotEqual(row['fingerprint'], previous_fingerprint)
        self.assertEqual(row['fingerprint'], app.fingerprint_file(row['source']))
        self.assertEqual(row['page_count'], 2)
        self.assertTrue(row['is_book'])
        self.assertEqual(len(app.PdfReader(row['snippet_path']).pages), 2)
        self.assertEqual(extract.call_args.args[1], True)
        self.assertEqual(extract.call_args.args[3], 'retry-model')

    def test_retry_failure_remains_retryable_with_new_error_and_snapshot(self):
        row = self.failure()
        retried, extract, _ = self.retry([row], error=RuntimeError('Provider still unavailable'))
        self.assertEqual(retried, [row])
        self.assertEqual(row['status'], 'Error')
        self.assertEqual(row['extraction_error'], 'Provider still unavailable')
        self.assertTrue(can_retry_extraction(row))
        self.assertTrue(row['needs_review'])
        self.assertEqual(Path(row['snippet_path']).read_bytes(), extract.call_args.args[0])
        self.assertEqual(row['fingerprint'], app.fingerprint_file(row['source']))

    def test_stop_before_retry_preserves_every_result(self):
        rows = [self.failure('First.pdf'), self.failure('Second.pdf')]
        before = copy.deepcopy(rows)
        retried, extract, progress = self.retry(rows, cancelled=lambda: True)
        self.assertEqual(retried, [])
        self.assertEqual(rows, before)
        self.assertEqual(progress, [])
        extract.assert_not_called()

    def test_stop_between_files_keeps_recovered_and_unattempted_failures(self):
        first, second = self.failure('First.pdf'), self.failure('Second.pdf')
        second_before = copy.deepcopy(second)
        stop = threading.Event()
        progress = []

        def report(index, total, row):
            progress.append((index, total, row))
            stop.set()

        retried, extract, _ = self.retry([first, second], cancelled=stop.is_set, on_progress=report)
        self.assertEqual(retried, [first])
        self.assertEqual(first['status'], 'Extracted')
        self.assertEqual(second, second_before)
        self.assertTrue(can_retry_extraction(second))
        self.assertEqual(extract.call_count, 1)
        self.assertEqual(progress, [(1, 2, first)])
        self.assertTrue(Path(first['source']).exists())

    def test_missing_metadata_after_retry_requires_review_without_more_api_retries(self):
        row = self.failure()
        metadata = dict(self.metadata, year='n.d.')
        self.retry([row], metadata=metadata)
        self.assertEqual(row['status'], 'Needs review')
        self.assertTrue(row['needs_review'])
        self.assertIn('year', row['reason'])
        self.assertNotIn('extraction_error', row)
        retried, extract, _ = self.retry([row])
        self.assertEqual(retried, [])
        extract.assert_not_called()

    def test_retry_progress_counts_failure_subset_without_replacing_full_list(self):
        first, completed, second = (self.failure('First.pdf'), self.failure('Done.pdf'),
                                    self.failure('Second.pdf'))
        completed.update(status='Renamed', metadata=copy.deepcopy(self.metadata), needs_review=False)
        rows = [first, completed, second]
        retried, extract, progress = self.retry(rows)
        self.assertEqual(retried, [first, second])
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows, [first, completed, second])
        self.assertEqual(extract.call_count, 2)
        self.assertEqual(progress, [(1, 2, first), (2, 2, second)])


if __name__ == '__main__':
    unittest.main()
