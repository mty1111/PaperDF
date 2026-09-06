"""Single-file reanalysis uses fresh pages without risking the saved result."""
import copy
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pdf_metadata_renamer as app
from paperdf_session import BatchStore, can_continue, validate_rows


class ReanalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = self.folder / 'Already named.pdf'
        writer = app.PdfWriter()
        for index in range(8):
            writer.add_blank_page(width=100 + index, height=200)
        with self.source.open('wb') as stream:
            writer.write(stream)
        self.store = BatchStore(self.folder / 'state')
        self.row = self.store.start([self.source], {'pages': 2, 'is_book': False,
            'naming': {'pattern': '{title}.pdf', 'author_format': '{surname}', 'unpublished': 'Unknown'}})[0]
        self.row.update(original_source=str(self.folder / 'garbled.pdf'), status='Renamed',
                        metadata={'title': 'Manual title', 'authors': ['Ada'], 'year': '1843'},
                        override='Manual name.pdf', fingerprint=app.fingerprint_file(self.source),
                        analysis_attempted=True, needs_review=False)
        self.row.pop('resume_action', None)
        self.meta = {'title': 'Later title', 'authors': ['Ada'], 'year': '1843',
                     'evidence': {'title': [{'page': 6, 'quote': 'Later title'}]}}

    def reanalyze(self, **kwargs):
        return app.reanalyze_row(self.row, 6, False, object(), 'fake',
                                kwargs.get('cancelled', lambda: False), self.folder / 'review')

    def test_more_pages_preserve_identity_and_require_review_after_restart(self):
        before = copy.deepcopy(self.row)
        with patch.object(app, 'get_metadata_from_snippet', return_value=self.meta) as extract:
            self.assertTrue(self.reanalyze())
        self.assertEqual(len(app.PdfReader(io.BytesIO(extract.call_args.args[0])).pages), 6)
        self.assertEqual(self.row['requested_pages'], 6)
        self.assertEqual(self.row['metadata'], self.meta)
        for key in ('source', 'original_source', 'row_id'):
            self.assertEqual(self.row[key], before[key])
        self.assertTrue(self.source.exists())
        self.assertFalse((self.folder / 'Later title.pdf').exists())
        self.assertNotIn('override', self.row)
        self.assertEqual(self.row['status'], 'Needs review')
        self.assertFalse(can_continue(self.row))
        self.store.save()
        restored = BatchStore(self.store.directory).load()['rows'][0]
        validate_rows([restored])
        self.assertEqual(restored['requested_pages'], 6)
        self.assertFalse(can_continue(restored))
        self.assertEqual(len(app.PdfReader(restored['snippet_path']).pages), 6)

    def test_failed_request_and_stop_before_start_preserve_original_result(self):
        before = copy.deepcopy(self.row)
        with patch.object(app, 'get_metadata_from_snippet', side_effect=TimeoutError('offline timeout')) as extract:
            self.assertFalse(self.reanalyze(cancelled=lambda: True))
            extract.assert_not_called()
            with self.assertRaisesRegex(ValueError, 'previous result kept'):
                self.reanalyze()
        self.assertEqual(self.row, before)

    def test_file_changed_during_request_preserves_previous_result(self):
        before = copy.deepcopy(self.row)
        def extract(*args):
            self.source.write_bytes(b'changed externally')
            return self.meta
        with patch.object(app, 'get_metadata_from_snippet', side_effect=extract):
            with self.assertRaisesRegex(ValueError, 'changed during extraction'):
                self.reanalyze()
        self.assertEqual(self.row, before)

    def test_interrupted_move_is_not_erased_by_reanalysis(self):
        self.row['pending_move'] = {'source': str(self.source)}
        before = copy.deepcopy(self.row)
        with patch.object(app, 'get_metadata_from_snippet') as extract:
            with self.assertRaisesRegex(ValueError, 'interrupted move'):
                self.reanalyze()
            extract.assert_not_called()
        self.assertEqual(self.row, before)

    def test_stop_during_request_keeps_completed_metadata_for_review(self):
        stopped = []
        def extract(*args):
            stopped.append(True)
            return self.meta
        with patch.object(app, 'get_metadata_from_snippet', side_effect=extract):
            self.assertTrue(self.reanalyze(cancelled=lambda: bool(stopped)))
        self.assertEqual(self.row['metadata'], self.meta)
        self.assertEqual(self.row['status'], 'Needs review')
        self.assertTrue(self.source.exists())
        self.assertFalse(can_continue(self.row))

    def test_continuation_reuses_per_file_page_count(self):
        self.row['requested_pages'] = 6
        with patch.object(app, 'get_metadata_from_snippet', return_value=self.meta) as extract:
            app.extract_rows([self.row], 2, False, object(), 'fake', lambda: False, lambda *args: None)
        self.assertEqual(len(app.PdfReader(io.BytesIO(extract.call_args.args[0])).pages), 6)


if __name__ == '__main__':
    unittest.main()
