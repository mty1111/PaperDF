import os
from pathlib import Path
import tempfile
import unittest

from paperdf_processing import correct_row, process_rows, record_results
from paperdf_workflow import fingerprint_file, undo_last_batch


class ProcessingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.history = str(self.folder / 'history')
        self.build = lambda meta: meta['title'] + '.pdf'

    def row(self, filename='RandomName.PDF', title='Paper', data=b'paper'):
        path = self.folder / filename
        path.write_bytes(data)
        return {'source': str(path), 'original_source': str(path), 'fingerprint': fingerprint_file(path),
                'selected': True, 'needs_review': False,
                'metadata': {'title': title, 'authors': ['Ada Lovelace'], 'year': '1843', 'journal': '', 'evidence': {}}}

    def test_complete_documents_rename_automatically_as_one_undo_batch(self):
        rows = [self.row(), self.row('Second.pdf', 'Other', b'other')]
        result = process_rows(rows, self.build, self.history)
        self.assertTrue(result.batch_id)
        self.assertEqual([r['status'] for r in rows], ['Renamed', 'Renamed'])
        self.assertEqual(len(list(Path(self.history).glob('*.json'))), 1)
        self.assertTrue(all(Path(r['source']).exists() for r in rows))
        undone = undo_last_batch(self.history)
        record_results(rows, undone.results)
        self.assertEqual([r['status'] for r in rows], ['Undone', 'Undone'])
        self.assertEqual(Path(rows[0]['source']).name, 'RandomName.PDF')

    def test_missing_metadata_stays_for_review_without_blocking_other_files(self):
        normal = self.row()
        incomplete = self.row('Incomplete.pdf', '', b'incomplete')
        incomplete.update(needs_review=True, selected=False, status='Needs review', message='Title missing')
        process_rows([normal, incomplete], self.build, self.history)
        self.assertEqual(normal['status'], 'Renamed')
        self.assertEqual(incomplete['status'], 'Needs review')
        self.assertTrue(Path(incomplete['source']).exists())

    def test_conflicts_are_deferred_and_suggestion_requires_review(self):
        row = self.row()
        occupied = self.folder / 'Paper.pdf'
        occupied.write_bytes(b'different')
        process_rows([row], self.build, self.history)
        self.assertEqual(row['status'], 'Needs review')
        self.assertTrue(Path(row['source']).exists())
        suggestion = row['suggested_name']
        correct_row(row, row['metadata'], suggestion, self.build, self.history)
        self.assertEqual(row['status'], 'Renamed')
        self.assertEqual(Path(row['source']).name, suggestion)
        self.assertEqual(occupied.read_bytes(), b'different')

    def test_renamed_document_can_be_corrected_then_both_batches_undone(self):
        row = self.row()
        process_rows([row], self.build, self.history)
        corrected = dict(row['metadata'], title='Corrected')
        correct_row(row, corrected, '', self.build, self.history)
        self.assertEqual(Path(row['source']).name, 'Corrected.pdf')
        record_results([row], undo_last_batch(self.history).results)
        self.assertEqual(Path(row['source']).name, 'Paper.pdf')
        record_results([row], undo_last_batch(self.history).results)
        self.assertEqual(Path(row['source']).name, 'RandomName.PDF')

    def test_review_refuses_changed_source_and_keeps_original_snapshot_fingerprint(self):
        row = self.row()
        before = row['fingerprint']
        Path(row['source']).write_bytes(b'external edits')
        correct_row(row, row['metadata'], '', self.build, self.history)
        self.assertEqual(row['status'], 'Failed')
        self.assertEqual(row['fingerprint'], before)
        self.assertTrue(Path(row['source']).exists())

    def test_stop_during_planning_never_starts_renames(self):
        row = self.row()
        result = process_rows([row], self.build, self.history, should_stop=lambda: True)
        self.assertIsNone(result.batch_id)
        self.assertEqual(row['status'], 'Stopped')
        self.assertTrue(Path(row['source']).exists())

    def test_duplicate_content_is_preserved_without_an_extra_rename(self):
        row = self.row()
        (self.folder / 'Paper.pdf').write_bytes(b'paper')
        process_rows([row], self.build, self.history)
        self.assertEqual(row['status'], 'Duplicate')
        self.assertTrue(Path(row['source']).exists())

    def test_deferred_target_is_not_reported_as_an_existing_duplicate(self):
        one, two = self.row(), self.row('Two.pdf')
        (self.folder / 'Paper.pdf').write_bytes(b'different')
        process_rows([one, two], self.build, self.history)
        self.assertEqual([one['status'], two['status']], ['Needs review', 'Needs review'])
        self.assertTrue(Path(one['source']).exists())
        self.assertTrue(Path(two['source']).exists())

    def test_successful_manual_repair_clears_old_extraction_error(self):
        row = self.row()
        row.update(reason='Extraction failed: quota exceeded', extraction_error='quota exceeded', needs_review=True)
        correct_row(row, row['metadata'], '', self.build, self.history)
        self.assertEqual(row['status'], 'Renamed')
        self.assertNotIn('reason', row)
        self.assertNotIn('extraction_error', row)


if __name__ == '__main__':
    unittest.main()
