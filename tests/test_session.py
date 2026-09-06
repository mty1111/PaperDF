"""Durable checkpoints, source validation, and real interrupted rename recovery."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from paperdf_processing import process_rows, record_results
from paperdf_session import BatchStore, SessionError, can_continue, remember_move, validate_rows
from paperdf_workflow import fingerprint_file, OperationResult, undo_last_batch


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = self.folder / 'Original.PDF'
        self.source.write_bytes(b'synthetic document')
        self.context = {'pages': 4, 'is_book': False,
                        'naming': {'pattern': '{title}.pdf', 'author_format': '{surname}', 'unpublished': 'Working paper'}}
        self.store = BatchStore(self.folder / 'state')
        self.rows = self.store.start([self.source], self.context)
        self.history = str(self.folder / 'history')

    def extracted(self):
        row = self.rows[0]
        row.update(metadata={'authors': ['Ada Lovelace'], 'year': '1843', 'journal': '', 'title': 'Notes'},
                   fingerprint=fingerprint_file(self.source), status='Extracted', needs_review=False,
                   selected=True, resume_action='rename', analysis_attempted=True)
        return row

    def reload(self):
        store = BatchStore(self.store.directory)
        data = store.load()
        validate_rows(data['rows'])
        return store, data['rows']

    def test_manifest_keeps_all_unique_inputs_before_extraction_and_no_credentials(self):
        other = self.folder / 'Pending.pdf'
        rows = self.store.start([self.source, other, self.source], dict(self.context, api_key='must-not-save'))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row['status'] == 'Pending' and not row['analysis_attempted'] for row in rows))
        self.assertNotIn('must-not-save', self.store.path.read_text())
        self.assertEqual(self.store.load()['context'], self.context)

    def test_snapshot_survives_temporary_session_cleanup(self):
        row = self.extracted()
        snapshot = self.folder / (uuid.uuid4().hex + '.pdf')
        snapshot.write_bytes(b'exact analyzed prefix')
        row['snippet_path'] = str(snapshot)
        self.store.save()
        snapshot.unlink()
        store, restored = self.reload()
        self.assertEqual(Path(restored[0]['snippet_path']).read_bytes(), b'exact analyzed prefix')
        self.assertEqual(restored[0]['status'], 'Stopped')
        self.assertEqual(restored[0]['resume_action'], 'rename')
        store.save()
        self.assertEqual(store.load()['rows'][0]['metadata'], row['metadata'])

    def test_failed_atomic_replace_keeps_last_checkpoint(self):
        before = self.store.path.read_bytes()
        self.extracted()
        with patch('paperdf_workflow.os.replace', side_effect=PermissionError('locked checkpoint')):
            with self.assertRaises(SessionError):
                self.store.save()
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.load()['rows'][0]['status'], 'Pending')

    def test_corrupt_or_incompatible_checkpoint_is_preserved(self):
        for content in ('{broken', json.dumps({'version': 99})):
            with self.subTest(content=content):
                self.store.path.write_text(content)
                with self.assertRaises(SessionError):
                    BatchStore(self.store.directory).load()
                self.assertEqual(self.store.path.read_text(), content)

    def test_external_snapshot_and_invalid_move_paths_are_rejected(self):
        original = json.loads(self.store.path.read_text())
        for kind in ('snapshot', 'move'):
            with self.subTest(kind=kind):
                data = copy.deepcopy(original)
                row = data['rows'][0]
                if kind == 'snapshot':
                    row['snippet_path'] = str(self.folder / 'outside.pdf')
                else:
                    row['fingerprint'] = fingerprint_file(self.source)
                    row['pending_move'] = {'source': row['source'], 'destination': str(self.folder / 'elsewhere' / 'file.pdf'),
                                           'action': 'rename'}
                self.store.path.write_text(json.dumps(data))
                with self.assertRaises(SessionError):
                    BatchStore(self.store.directory).load()

    def test_invalid_per_file_page_counts_are_rejected(self):
        original = json.loads(self.store.path.read_text())
        for pages in (0, 51, True, '6'):
            with self.subTest(pages=pages):
                data = copy.deepcopy(original)
                data['rows'][0]['requested_pages'] = pages
                self.store.path.write_text(json.dumps(data))
                with self.assertRaises(SessionError):
                    BatchStore(self.store.directory).load()

    def test_changed_file_retains_old_evidence_but_requires_fresh_extraction(self):
        row = self.extracted()
        old_hash = row['fingerprint']
        self.source.write_bytes(b'new contents')
        validate_rows(self.rows)
        self.assertEqual(row['status'], 'Changed')
        self.assertEqual(row['resume_action'], 'extract')
        self.assertEqual(row['fingerprint'], old_hash)
        self.assertEqual(row['metadata']['title'], 'Notes')
        self.assertTrue(can_continue(row))

    def test_missing_file_is_not_guessed_and_original_state_returns_when_restored(self):
        row = self.extracted()
        moved = self.folder / 'External move.pdf'
        self.source.rename(moved)
        validate_rows(self.rows)
        self.assertEqual(row['status'], 'Missing')
        self.assertNotIn('resume_action', row)
        self.assertEqual(row['source'], str(self.source))
        moved.rename(self.source)
        validate_rows(self.rows)
        self.assertEqual(row['status'], 'Stopped')
        self.assertEqual(row['resume_action'], 'rename')

    def test_stopped_validation_requires_checks_before_reusing_results(self):
        row = self.extracted()
        validate_rows(self.rows, lambda: True)
        self.assertEqual(row['status'], 'Unverified')
        self.assertNotIn('resume_action', row)
        validate_rows(self.rows)
        self.assertEqual(row['resume_action'], 'rename')

    def test_completed_and_review_results_do_not_become_resumable(self):
        row = self.extracted()
        for status in ('Renamed', 'Undone', 'Duplicate', 'Unchanged', 'Needs review'):
            with self.subTest(status=status):
                row.update(status=status, needs_review=status == 'Needs review')
                row.pop('resume_action', None)
                before = copy.deepcopy(row)
                validate_rows(self.rows)
                self.assertEqual(row, before)
                self.assertFalse(can_continue(row))

    def before_rename(self, result):
        remember_move(self.rows, result, 'rename')
        self.store.save()

    def test_crash_after_rename_before_result_checkpoint_recovers_without_second_rename(self):
        self.extracted()
        self.store.save()
        process_rows(self.rows, lambda m: m['title'] + '.pdf', self.history, before_move=self.before_rename)
        # No post-result checkpoint: disk still has the intent saved before the move.
        store, rows = self.reload()
        self.assertEqual(rows[0]['status'], 'Renamed')
        self.assertEqual(Path(rows[0]['source']).name, 'Notes.pdf')
        self.assertFalse(can_continue(rows[0]))
        self.assertEqual(rows[0]['original_source'], str(self.source))
        store.save()
        self.assertNotIn('pending_move', store.load()['rows'][0])

    def test_failed_checkpoint_before_rename_prevents_file_mutation(self):
        self.extracted()
        def fail(result):
            raise SessionError('checkpoint disk full')
        with self.assertRaises(SessionError):
            process_rows(self.rows, lambda m: m['title'] + '.pdf', self.history, before_move=fail)
        self.assertTrue(self.source.exists())
        self.assertFalse((self.folder / 'Notes.pdf').exists())

    def test_saved_intent_before_unstarted_move_reuses_metadata(self):
        row = self.extracted()
        destination = str(self.folder / 'Notes.pdf')
        self.before_rename(OperationResult(str(self.source), destination, 'pending'))
        _, restored = self.reload()
        self.assertEqual(restored[0]['source'], str(self.source))
        self.assertEqual(restored[0]['status'], 'Stopped')
        self.assertEqual(restored[0]['metadata'], row['metadata'])
        self.assertNotIn('pending_move', restored[0])

    def test_ambiguous_interrupted_move_is_held_for_recovery(self):
        row = self.extracted()
        destination = self.folder / 'Notes.pdf'
        self.before_rename(OperationResult(str(self.source), str(destination), 'pending'))
        destination.write_bytes(self.source.read_bytes())
        _, restored = self.reload()
        self.assertEqual(restored[0]['status'], 'Recovery needed')
        self.assertNotIn('resume_action', restored[0])
        self.assertTrue(self.source.exists())
        self.assertTrue(destination.exists())

    def test_crash_after_undo_restores_path_without_reapplying_it(self):
        self.extracted()
        process_rows(self.rows, lambda m: m['title'] + '.pdf', self.history, before_move=self.before_rename)
        self.store.save()
        def before_undo(result):
            remember_move(self.rows, result, 'undo')
            self.store.save()
        undo_last_batch(self.history, before_move=before_undo)
        _, restored = self.reload()
        self.assertEqual(restored[0]['source'], str(self.source))
        self.assertEqual(restored[0]['status'], 'Undone')
        self.assertFalse(can_continue(restored[0]))

    def test_unstarted_undo_restores_completed_status_after_a_temporary_missing_file(self):
        self.extracted()
        process_rows(self.rows, lambda m: m['title'] + '.pdf', self.history, before_move=self.before_rename)
        row = self.rows[0]
        remember_move(self.rows, OperationResult(row['source'], str(self.source), 'pending'), 'undo')
        current = Path(row['source'])
        hidden = self.folder / 'Temporarily moved.pdf'
        current.rename(hidden)
        validate_rows(self.rows)
        self.assertEqual(row['status'], 'Recovery needed')
        hidden.rename(current)
        validate_rows(self.rows)
        self.assertEqual(row['status'], 'Renamed')
        self.assertNotIn('pending_move', row)
        self.assertFalse(can_continue(row))

    def test_new_batch_removes_only_previous_generated_cache(self):
        old_cache = self.store._cache_dir(self.store.data['batch_id'])
        generated = old_cache / (uuid.uuid4().hex + '.pdf')
        generated.write_bytes(b'old snapshot')
        unrelated = self.store.directory / 'personal.pdf'
        unrelated.write_bytes(b'keep')
        self.store.start([self.source], self.context)
        self.assertFalse(old_cache.exists())
        self.assertEqual(unrelated.read_bytes(), b'keep')

    def test_checkpoint_can_be_restored_by_a_separate_process(self):
        self.extracted()
        self.store.save()
        script = ('import sys; from paperdf_session import BatchStore, validate_rows; '
                  's=BatchStore(sys.argv[1]); d=s.load(); validate_rows(d["rows"]); '
                  'assert d["rows"][0]["status"] == "Stopped"; '
                  'assert d["rows"][0]["metadata"]["title"] == "Notes"; print("restored")')
        result = subprocess.run([sys.executable, '-c', script, str(self.store.directory)],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'restored')


if __name__ == '__main__':
    unittest.main()
