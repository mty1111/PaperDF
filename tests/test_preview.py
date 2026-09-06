"""Offline integration checks for preview extraction and the actual Tk widgets."""
import os
import copy
import csv
import gc
from pathlib import Path
import queue
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import Mock, patch

_config = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {'LOCALAPPDATA': _config.name, 'GEMINI_API_KEY': ''}):
    import pdf_metadata_renamer as app
from paperdf_preview import ResultsPanel
from paperdf_workflow import fingerprint_file
from paperdf_session import BatchStore


def destroy_test_root(root):
    # Tcl timers outlive a destroyed Tk window. On macOS their later bgerror
    # opens a native modal dialog and stalls another test's event loop.
    for callback in root.tk.splitlist(root.tk.call('after', 'info')):
        root.after_cancel(callback)
    root.destroy()


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / 'paper.pdf'
        writer = app.PdfWriter()
        writer.add_blank_page(width=100, height=100)
        with self.source.open('wb') as stream:
            writer.write(stream)

    def test_preview_calls_extraction_once_and_leaves_original_untouched(self):
        before = self.source.read_bytes()
        metadata = {'authors': ['Ada Lovelace'], 'year': '1843', 'journal': '', 'title': 'Notes'}
        with patch.object(app, 'get_metadata_from_snippet', return_value=metadata) as extract:
            rows = app.prepare_preview([str(self.source), str(self.source)], 4, False, object(), 'fake', lambda: False, lambda *args: None)
        self.assertEqual(extract.call_count, 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['metadata'], metadata)
        self.assertTrue(rows[0]['selected'])
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(list(self.source.parent.iterdir()), [self.source])

    def test_extraction_failure_stays_editable_and_stop_keeps_completed_rows(self):
        event = threading.Event()
        with patch.object(app, 'get_metadata_from_snippet', side_effect=ValueError('Invalid JSON')):
            rows = app.prepare_preview([str(self.source), str(self.source.parent / 'next.pdf')], 4, False,
                                       object(), 'fake', event.is_set, lambda *args: event.set())
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]['selected'])
        self.assertEqual(rows[0]['fingerprint'], fingerprint_file(self.source))
        self.assertEqual(rows[0]['extraction_error'], 'Invalid JSON')

    def test_uploaded_snippet_is_deleted_on_parse_failure(self):
        client = Mock()
        client.files.upload.return_value.name = 'files/test-snippet'
        client.models.generate_content.return_value.text = 'bad json'
        with self.assertRaises(ValueError):
            app.get_metadata_from_snippet(b'pdf snippet', False, client, 'fake')
        client.files.delete.assert_called_once_with(name='files/test-snippet')
        self.assertEqual(client.models.generate_content.call_args.kwargs['model'], 'fake')

    def test_naming_uses_review_snapshot_even_if_settings_change(self):
        naming = {'pattern': '{authors} - {title}.pdf', 'author_format': '{surname}', 'unpublished': 'Working Paper'}
        metadata = {'authors': ['Ada Lovelace'], 'title': 'Notes'}
        with patch.object(app, 'OUTPUT_PATTERN', 'different.pdf'), patch.object(app, 'AUTHOR_FMT_PAPER', '{first}'):
            self.assertEqual(app.build_new_filename(metadata, False, naming), 'Lovelace - Notes.pdf')

    def test_extraction_preserves_original_path_case(self):
        mixed = self.source.with_name('MyPaper.PDF')
        self.source.rename(mixed)
        with patch.object(app, 'get_metadata_from_snippet', return_value={'title': 'Notes'}):
            rows = app.prepare_preview([str(mixed)], 1, False, object(), 'fake', lambda: False, lambda *args: None)
        self.assertEqual(Path(rows[0]['source']).name, 'MyPaper.PDF')


class ResultsWidgetTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f'Tk display unavailable: {exc}')
        self.root.withdraw()
        self.addCleanup(destroy_test_root, self.root)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.events = queue.Queue()
        self.logs = []
        self.busy = False
        error_patch = patch('paperdf_preview.messagebox.showerror')
        self.dialog_errors = error_patch.start()
        self.addCleanup(error_patch.stop)

        def set_busy(value):
            self.busy = value
            self.panel.set_enabled(not value)

        self.panel = ResultsPanel(self.root, str(Path(self.temp.name) / 'history'), self.events.put,
                                  set_busy, self.logs.append, threading.Event())
        self.panel.pack(fill='both', expand=True)

    def pump(self):
        deadline = time.monotonic() + 10
        while True:
            self.root.update()
            try:
                while True:
                    self.events.get_nowait()()
            except queue.Empty:
                pass
            if not self.busy and self.events.empty():
                if self.dialog_errors.called:
                    self.fail(f'Unexpected error dialog: {self.dialog_errors.call_args}')
                return
            if time.monotonic() > deadline:
                self.fail('Background operation did not finish')
            time.sleep(0.01)

    def row(self, original='input.pdf', title='Reviewed', content=b'original PDF content'):
        path = Path(self.temp.name) / original
        path.write_bytes(content)
        return {'source': str(path), 'original_source': str(path),
                'metadata': {'authors': ['Ada Lovelace'], 'year': '1843', 'journal': '', 'title': title},
                'selected': True, 'needs_review': False, 'fingerprint': fingerprint_file(path)}

    def load(self, rows):
        self.panel.load(rows, lambda meta: meta['title'] + '.pdf', False)
        self.pump()

    def test_default_automatically_renames_without_preapproval_table(self):
        row = self.row()
        self.load([row])
        self.assertEqual(row['status'], 'Renamed')
        self.assertIn('Renamed: 1 / 1 files', self.panel.summary.get())
        self.assertEqual(Path(row['source']).name, 'Reviewed.pdf')
        self.assertFalse(hasattr(self.panel, 'apply_btn'))
        self.assertEqual(self.panel.tree['columns'], ('title', 'authors', 'year', 'journal', 'status'))

    def test_successful_result_can_be_reviewed_and_corrected_locally(self):
        row = self.row()
        self.load([row])
        with patch('paperdf_preview.ReviewDialog') as dialog:
            self.panel.review_btn.invoke()
            self.assertEqual(dialog.call_args.args[1]['source'], row['source'])
            callback = dialog.call_args.args[3]
            callback(dict(row['metadata'], title='Corrected'), '')
        self.pump()
        self.assertEqual(Path(row['source']).name, 'Corrected.pdf')
        self.assertIn('Renamed: 1 / 1 files', self.panel.summary.get())
        self.panel.apply_correction(row, row['metadata'], '')
        self.pump()
        self.assertEqual(row['status'], 'Unchanged')
        self.assertIn('Renamed: 1 / 1 files', self.panel.summary.get())
        self.panel.undo_btn.invoke()
        self.pump()
        self.assertEqual(Path(row['source']).name, 'Reviewed.pdf')
        self.assertIn('Renamed: 1 / 1 files', self.panel.summary.get())
        self.panel.undo_btn.invoke()
        self.pump()
        self.assertEqual(Path(row['source']).name, 'input.pdf')
        self.assertIn('Renamed: 0 / 1 files', self.panel.summary.get())

    def test_attention_filter_prioritizes_incomplete_rows(self):
        normal, incomplete = self.row(), self.row('Second.pdf', '', b'second')
        incomplete.update(needs_review=True, status='Needs review')
        self.load([normal, incomplete])
        self.assertEqual(self.panel.tree.get_children()[0], '1')
        self.panel.attention_only.set(True)
        self.panel._render()
        self.assertEqual(self.panel.tree.get_children(), ('1',))
        self.assertEqual(incomplete['status'], 'Needs review')

    def test_no_history_undo_preserves_current_results(self):
        row = self.row()
        row.update(needs_review=True, status='Needs review')
        self.load([row])
        self.panel.undo_btn.invoke()
        self.pump()
        self.assertEqual(row['status'], 'Needs review')

    def test_export_includes_filtered_out_results_and_current_renamed_path(self):
        normal, incomplete = self.row(), self.row('Second.pdf', '', b'second')
        incomplete.update(needs_review=True, status='Needs review', message='Title missing')
        self.load([normal, incomplete])
        self.panel.attention_only.set(True)
        self.panel._render()
        self.assertEqual(self.panel.tree.get_children(), ('1',))
        before = copy.deepcopy(self.panel.rows)
        target = Path(self.temp.name) / 'report.csv'
        with patch('paperdf_preview.filedialog.asksaveasfilename', return_value=str(target)):
            self.panel.export_btn.invoke()
        with target.open(encoding='utf-8-sig', newline='') as stream:
            results = list(csv.DictReader(stream))
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]['current_path'], normal['source'])
        self.assertEqual(Path(results[0]['original_path']).name, 'input.pdf')
        self.assertEqual(results[1]['details'], 'Title missing')
        self.assertEqual(self.panel.rows, before)
        self.assertTrue(any('Exported 2 file results' in line for line in self.logs))

    def test_export_cancel_and_busy_state_do_not_write_reports(self):
        self.assertEqual(str(self.panel.export_btn['state']), 'disabled')
        self.load([self.row()])
        with patch('paperdf_preview.filedialog.asksaveasfilename', return_value='') as choose, \
             patch('paperdf_preview.export_results') as export:
            self.panel.export_btn.invoke()
            choose.assert_called_once()
            export.assert_not_called()
            choose.reset_mock()
            self.panel.set_enabled(False)
            self.assertEqual(str(self.panel.export_btn['state']), 'disabled')
            self.panel.export_all()
            choose.assert_not_called()
            export.assert_not_called()

    def test_export_error_is_reported_without_changing_results(self):
        self.load([self.row()])
        before = copy.deepcopy(self.panel.rows)
        with patch('paperdf_preview.filedialog.asksaveasfilename', return_value='report.csv'), \
             patch('paperdf_preview.export_results', side_effect=PermissionError('File locked')):
            self.panel.export_btn.invoke()
        self.dialog_errors.assert_called_once()
        self.assertEqual(self.dialog_errors.call_args.args, ('Export failed', 'File locked'))
        self.assertEqual(self.panel.rows, before)
        self.assertFalse(any('Exported' in line for line in self.logs))

    def test_stopped_extraction_does_not_start_auto_rename(self):
        row = self.row()
        self.panel.stop_event.set()
        self.panel.load([row], lambda meta: meta['title'] + '.pdf', False, total_files=10)
        self.pump()
        self.assertEqual(row['status'], 'Stopped')
        self.assertIn('Renamed: 0 / 10 files', self.panel.summary.get())
        self.assertTrue(Path(row['source']).exists())

    def test_conflict_remains_unchanged_until_document_review(self):
        row = self.row()
        (Path(self.temp.name) / 'Reviewed.pdf').write_bytes(b'occupied')
        self.load([row])
        self.assertEqual(row['status'], 'Needs review')
        self.assertEqual(Path(row['source']).name, 'input.pdf')
        with patch('paperdf_preview.ReviewDialog') as dialog:
            self.panel.review_btn.invoke()
            self.assertEqual(dialog.call_args.args[1]['override'], row['suggested_name'])


class MainWindowTests(unittest.TestCase):
    def setUp(self):
        # These tests create multiple Tcl interpreters. Collect their widget cycles
        # on the UI thread, never in a later test's extraction worker.
        gc.collect()
        enabled = gc.isenabled()
        gc.disable()
        if enabled:
            self.addCleanup(gc.enable)
        self.addCleanup(gc.collect)

    def run_retry_window(self, directory, advance, metadata, client):
        """Drive the actual main window with synthetic PDFs and offline providers."""
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f'Tk display unavailable: {exc}')
        root.withdraw()
        errors = []
        state = {'phase': 'start'}

        def widgets(parent):
            for child in parent.winfo_children():
                yield child
                yield from widgets(child)

        def tick():
            try:
                all_widgets = list(widgets(root))
                panel = next(w for w in all_widgets if isinstance(w, ResultsPanel))
                buttons = {w.cget('text'): w for w in all_widgets if isinstance(w, ttk.Button)}
                labels = [w.cget('text') for w in all_widgets if isinstance(w, ttk.Label)]
                if not panel.busy and advance(state, panel, buttons, labels, all_widgets):
                    destroy_test_root(root)
                    return
                root.after(10, tick)
            except Exception as exc:
                errors.append(exc)
                destroy_test_root(root)

        def timeout():
            errors.append(AssertionError(f'Retry workflow timed out at {state["phase"]}'))
            destroy_test_root(root)

        root.after(10, tick)
        root.after(10000, timeout)
        with patch.object(app.tk, 'Tk', return_value=root), patch.object(app, 'FIRST_RUN', False), \
             patch.object(app, 'STORE_DIR', directory), patch.object(app, 'API_KEY', 'initial-key'), \
             patch.object(app, 'MODEL_NAME', 'initial-model'), \
             patch.object(app, 'OUTPUT_PATTERN', '{title}.pdf'), \
             patch.object(app.genai, 'Client', client), \
             patch.object(app, 'get_metadata_from_snippet', metadata), \
             patch.object(app.messagebox, 'showerror') as dialogs:
            app.main()
        self.assertFalse(errors, errors)
        self.assertEqual(state['phase'], 'complete')
        return dialogs

    def make_sources(self, directory, count):
        paths = []
        for index in range(count):
            path = Path(directory) / f'Input{index}.pdf'
            writer = app.PdfWriter()
            for _ in range(4):
                writer.add_blank_page(width=100 + index, height=100)
            with path.open('wb') as stream:
                writer.write(stream)
            paths.append(str(path))
        return paths

    def test_retry_keeps_corrections_batch_counts_and_has_separate_undo(self):
        meta = {'authors': ['Ada Lovelace'], 'year': '1843', 'journal': '', 'title': 'First'}
        extraction = Mock(side_effect=[meta, TimeoutError('offline timeout'),
                                       dict(meta, year='n.d.'), dict(meta, title='Recovered')])
        clients = [Mock(), Mock()]
        client = Mock(side_effect=clients)
        with tempfile.TemporaryDirectory() as directory:
            sources = self.make_sources(directory, 3)

            def advance(state, panel, buttons, labels, widgets):
                if state['phase'] == 'start':
                    app.selected_files[:] = sources
                    buttons['Process PDFs'].invoke()
                    state['phase'] = 'processed'
                elif state['phase'] == 'processed':
                    self.assertEqual([r['status'] for r in panel.rows], ['Renamed', 'Error', 'Needs review'])
                    self.assertIn('Renamed: 1 / 3 files  ·  Analyzed: 3 / 3', labels)
                    panel.apply_correction(panel.rows[0], dict(meta, title='Corrected'), '')
                    state['phase'] = 'corrected'
                elif state['phase'] == 'corrected':
                    state['kept'] = copy.deepcopy([panel.rows[0], panel.rows[2]])
                    state['list'] = panel.rows
                    # Changing inputs/settings must not change this batch's page/naming context.
                    page_entry = next(w for w in widgets if isinstance(w, ttk.Entry) and w.get() == '4')
                    page_entry.delete(0, tk.END)
                    page_entry.insert(0, '1')
                    app.API_KEY, app.MODEL_NAME, app.OUTPUT_PATTERN = 'fixed-key', 'fixed-model', 'Wrong.pdf'
                    panel.retry_btn.invoke()
                    self.assertTrue(panel.busy)
                    self.assertEqual(str(panel.retry_btn['state']), 'disabled')
                    self.assertTrue(any(isinstance(w, ttk.Label) and w.cget('text') ==
                                        'Retrying: 0 / 1  ·  Renamed: 1 / 3 files  ·  Analyzed: 3 / 3'
                                        for w in widgets))
                    state['phase'] = 'retried'
                elif state['phase'] == 'retried':
                    self.assertIs(panel.rows, state['list'])
                    self.assertEqual([panel.rows[0], panel.rows[2]], state['kept'])
                    self.assertEqual(Path(panel.rows[1]['source']).name, 'Recovered.pdf')
                    self.assertEqual(panel.rows[1]['page_count'], 4)
                    self.assertIn('Renamed: 2 / 3 files  ·  Analyzed: 3 / 3', labels)
                    self.assertEqual(str(panel.retry_btn['state']), 'disabled')
                    self.assertEqual(app.selected_files, [r['source'] for r in panel.rows])
                    self.assertEqual(extraction.call_count, 4)
                    self.assertEqual(extraction.call_args.args[3], 'fixed-model')
                    self.assertEqual(client.call_args.kwargs['api_key'], 'fixed-key')
                    panel.undo_btn.invoke()
                    state['phase'] = 'undone'
                elif state['phase'] == 'undone':
                    self.assertEqual(panel.rows[0], state['kept'][0])
                    self.assertEqual(panel.rows[1]['source'], sources[1])
                    self.assertIn('Renamed: 1 / 3 files  ·  Analyzed: 3 / 3', labels)
                    self.assertEqual(extraction.call_count, 4)
                    state['phase'] = 'complete'
                    return True

            dialogs = self.run_retry_window(directory, advance, extraction, client)
        self.assertFalse(dialogs.called, dialogs.call_args)
        for sdk in clients:
            sdk.close.assert_called_once()

    def test_initial_and_retry_client_failures_preserve_inputs_until_recovery(self):
        extraction = Mock(side_effect=[{'authors': ['Ada'], 'year': '2024', 'title': title}
                                       for title in ('Recovered A', 'Recovered B')])
        sdk = Mock()
        client = Mock(side_effect=[ValueError('initial client error'), ValueError('retry client error'), sdk])
        with tempfile.TemporaryDirectory() as directory:
            sources = self.make_sources(directory, 2)

            def advance(state, panel, buttons, labels, widgets):
                if state['phase'] == 'start':
                    app.selected_files[:] = sources + sources[:1]
                    buttons['Process PDFs'].invoke()
                    state['phase'] = 'initial_error'
                elif state['phase'] in ('initial_error', 'retry_error'):
                    self.assertEqual([r['source'] for r in panel.rows], sources)
                    self.assertTrue(all(r['status'] == 'Error' for r in panel.rows))
                    self.assertIn('Renamed: 0 / 2 files  ·  Analyzed: 0 / 2', labels)
                    self.assertEqual(str(panel.retry_btn['state']), 'normal')
                    self.assertEqual(extraction.call_count, 0)
                    panel.retry_btn.invoke()
                    state['phase'] = 'retry_error' if state['phase'] == 'initial_error' else 'recovered'
                elif state['phase'] == 'recovered':
                    self.assertTrue(all(r['status'] == 'Renamed' for r in panel.rows))
                    self.assertIn('Renamed: 2 / 2 files  ·  Analyzed: 2 / 2', labels)
                    self.assertEqual(extraction.call_count, 2)
                    self.assertEqual(client.call_count, 3)
                    state['phase'] = 'complete'
                    return True

            dialogs = self.run_retry_window(directory, advance, extraction, client)
        self.assertEqual(dialogs.call_count, 2)
        sdk.close.assert_called_once()

    def test_reanalysis_selected_file_preserves_batch_and_undo(self):
        meta = {'authors': ['Ada'], 'year': '2024'}
        extraction = Mock(side_effect=[dict(meta, title=title) for title in ('First', 'Second', 'New title')])
        with tempfile.TemporaryDirectory() as directory:
            sources = self.make_sources(directory, 2)

            def advance(state, panel, buttons, labels, widgets):
                if state['phase'] == 'start':
                    app.selected_files[:] = sources
                    buttons['Process PDFs'].invoke()
                    state['phase'] = 'processed'
                elif state['phase'] == 'processed':
                    state['other'] = copy.deepcopy(panel.rows[1])
                    state['path'] = panel.rows[0]['source']
                    panel.tree.selection_set('0')
                    with patch.object(app.simpledialog, 'askinteger', return_value=None):
                        panel.reanalyze_btn.invoke()
                    self.assertEqual(extraction.call_count, 2)
                    with patch.object(app.simpledialog, 'askinteger', return_value=3):
                        panel.reanalyze_btn.invoke()
                    self.assertEqual(str(panel.reanalyze_btn['state']), 'disabled')
                    state['phase'] = 'reanalyzed'
                elif state['phase'] == 'reanalyzed':
                    self.assertEqual(extraction.call_count, 3)
                    self.assertEqual(panel.rows[1], state['other'])
                    self.assertEqual(panel.rows[0]['source'], state['path'])
                    self.assertEqual(panel.rows[0]['page_count'], 3)
                    self.assertEqual(panel.rows[0]['status'], 'Needs review')
                    self.assertIn('Renamed: 2 / 2 files  ·  Analyzed: 2 / 2', labels)
                    self.assertEqual(str(panel.continue_btn['state']), 'disabled')
                    panel.apply_correction(panel.rows[0], panel.rows[0]['metadata'], '')
                    state['phase'] = 'corrected'
                elif state['phase'] == 'corrected':
                    self.assertEqual(Path(panel.rows[0]['source']).name, 'New title.pdf')
                    self.assertEqual(extraction.call_count, 3)
                    panel.undo_btn.invoke()
                    state['phase'] = 'undone'
                elif state['phase'] == 'undone':
                    self.assertEqual(panel.rows[0]['source'], state['path'])
                    self.assertEqual(panel.rows[1], state['other'])
                    self.assertIn('Renamed: 2 / 2 files  ·  Analyzed: 2 / 2', labels)
                    state['phase'] = 'complete'
                    return True

            dialogs = self.run_retry_window(directory, advance, extraction, Mock(return_value=Mock()))
            self.assertFalse(dialogs.called, dialogs.call_args)
            saved = BatchStore(Path(directory) / 'batch-state').load()['rows']
            self.assertEqual(saved[0]['requested_pages'], 3)

    def test_stop_during_retry_keeps_fresh_metadata_and_remaining_errors(self):
        meta = {'authors': ['Ada'], 'year': '2024', 'title': 'First'}
        attempts = []

        def extract(*args):
            attempts.append(args)
            if len(attempts) in (2, 3):
                raise TimeoutError('offline timeout')
            if len(attempts) == 4:
                app.stop_event.set()
                return dict(meta, title='Stopped recovery')
            return dict(meta, title='Last recovery') if len(attempts) == 5 else meta

        with tempfile.TemporaryDirectory() as directory:
            sources = self.make_sources(directory, 3)

            def advance(state, panel, buttons, labels, widgets):
                if state['phase'] == 'start':
                    app.selected_files[:] = sources
                    buttons['Process PDFs'].invoke()
                    state['phase'] = 'processed'
                elif state['phase'] == 'processed':
                    state['kept'] = copy.deepcopy(panel.rows[0])
                    panel.retry_btn.invoke()
                    state['phase'] = 'stopped'
                elif state['phase'] == 'stopped':
                    self.assertEqual([r['status'] for r in panel.rows], ['Renamed', 'Stopped', 'Error'])
                    self.assertEqual(panel.rows[1]['metadata']['title'], 'Stopped recovery')
                    self.assertTrue(Path(sources[1]).exists())
                    self.assertEqual(len(attempts), 4)
                    self.assertIn('Renamed: 1 / 3 files  ·  Analyzed: 3 / 3', labels)
                    self.assertEqual(str(panel.retry_btn['state']), 'normal')
                    panel.retry_btn.invoke()
                    state['phase'] = 'remaining_retried'
                elif state['phase'] == 'remaining_retried':
                    self.assertEqual(panel.rows[0], state['kept'])
                    self.assertEqual([r['status'] for r in panel.rows], ['Renamed', 'Stopped', 'Renamed'])
                    self.assertEqual(len(attempts), 5)
                    self.assertIn('Renamed: 2 / 3 files  ·  Analyzed: 3 / 3', labels)
                    state['phase'] = 'complete'
                    return True

            dialogs = self.run_retry_window(directory, advance, Mock(side_effect=extract), Mock(return_value=Mock()))
        self.assertFalse(dialogs.called, dialogs.call_args)

    def test_restart_resumes_pending_files_and_reuses_saved_metadata(self):
        meta = {'authors': ['Ada'], 'year': '2024', 'title': 'Cached first'}
        saved_snapshots = []
        def first_extract(*args):
            app.stop_event.set()
            return meta

        with tempfile.TemporaryDirectory() as directory:
            sources = self.make_sources(directory, 2)
            initial = Mock(side_effect=first_extract)

            def stop_and_close(state, panel, buttons, labels, widgets):
                if state['phase'] == 'start':
                    app.selected_files[:] = sources
                    buttons['Process PDFs'].invoke()
                    state['phase'] = 'stopped'
                elif state['phase'] == 'stopped':
                    self.assertEqual([r['status'] for r in panel.rows], ['Stopped', 'Pending'])
                    self.assertEqual(initial.call_count, 1)
                    self.assertIn('Renamed: 0 / 2 files  ·  Analyzed: 1 / 2', labels)
                    self.assertEqual(str(panel.continue_btn['state']), 'normal')
                    saved_snapshots.append(panel.rows[0]['snippet_path'])
                    state['phase'] = 'complete'
                    return True

            dialogs = self.run_retry_window(directory, stop_and_close, initial, Mock(return_value=Mock()))
            self.assertFalse(dialogs.called, dialogs.call_args)
            self.assertFalse(Path(saved_snapshots[0]).exists())  # temporary copy removed
            durable = BatchStore(Path(directory) / 'batch-state').load()
            self.assertTrue(Path(durable['rows'][0]['snippet_path']).exists())
            saved_snapshots.append(durable['rows'][0]['snippet_path'])
            resumed = Mock(return_value=dict(meta, title='Fresh second'))
            client = Mock(return_value=Mock())

            def restore_and_continue(state, panel, buttons, labels, widgets):
                if state['phase'] == 'start':
                    self.assertEqual([r['status'] for r in panel.rows], ['Stopped', 'Pending'])
                    self.assertEqual(panel.rows[0]['snippet_path'], saved_snapshots[1])
                    self.assertEqual(app.selected_files, sources)
                    resumed.assert_not_called()
                    client.assert_not_called()
                    panel.continue_btn.invoke()
                    state['phase'] = 'continued'
                elif state['phase'] == 'continued':
                    self.assertEqual([r['status'] for r in panel.rows], ['Renamed', 'Renamed'])
                    self.assertEqual([Path(r['source']).name for r in panel.rows], ['Cached first.pdf', 'Fresh second.pdf'])
                    self.assertEqual(resumed.call_count, 1)
                    self.assertEqual(panel.rows[0]['metadata'], meta)
                    self.assertIn('Renamed: 2 / 2 files  ·  Analyzed: 2 / 2', labels)
                    panel.undo_btn.invoke()
                    state['phase'] = 'undone'
                elif state['phase'] == 'undone':
                    self.assertEqual([r['status'] for r in panel.rows], ['Undone', 'Undone'])
                    self.assertEqual(app.selected_files, sources)
                    self.assertIn('Renamed: 0 / 2 files  ·  Analyzed: 2 / 2', labels)
                    state['phase'] = 'complete'
                    return True

            dialogs = self.run_retry_window(directory, restore_and_continue, resumed, client)
            self.assertFalse(dialogs.called, dialogs.call_args)
            durable = BatchStore(Path(directory) / 'batch-state').load()
            self.assertEqual([r['status'] for r in durable['rows']], ['Undone', 'Undone'])

    def test_restored_cache_finishes_locally_without_an_api_key(self):
        meta = {'authors': ['Ada'], 'year': '2024', 'title': 'Cached'}
        with tempfile.TemporaryDirectory() as directory:
            sources = self.make_sources(directory, 1)
            store = BatchStore(Path(directory) / 'batch-state')
            rows = store.start(sources, {'pages': 4, 'is_book': False,
                               'naming': {'pattern': '{title}.pdf', 'author_format': '{surname}', 'unpublished': 'Unknown'}})
            with patch.object(app, 'get_metadata_from_snippet', return_value=meta):
                app.extract_rows(rows, 4, False, Mock(), 'fake', lambda: False, lambda *args: store.save())
            extraction, client = Mock(), Mock()

            def advance(state, panel, buttons, labels, widgets):
                if state['phase'] == 'start':
                    self.assertEqual(panel.rows[0]['status'], 'Stopped')
                    app.API_KEY = ''
                    panel.continue_btn.invoke()
                    state['phase'] = 'continued'
                elif state['phase'] == 'continued':
                    self.assertEqual(panel.rows[0]['status'], 'Renamed')
                    self.assertEqual(Path(panel.rows[0]['source']).name, 'Cached.pdf')
                    self.assertIn('Renamed: 1 / 1 files  ·  Analyzed: 1 / 1', labels)
                    extraction.assert_not_called()
                    client.assert_not_called()
                    state['phase'] = 'complete'
                    return True

            dialogs = self.run_retry_window(directory, advance, extraction, client)
            self.assertFalse(dialogs.called, dialogs.call_args)

    def test_new_batch_reuses_cache_without_key_and_applies_new_naming(self):
        metadata = Mock(return_value={'authors': ['Ada'], 'year': '1843', 'title': 'Cached title'})
        client = Mock(return_value=Mock())
        with tempfile.TemporaryDirectory() as directory:
            sources = self.make_sources(directory, 1)

            def advance(state, panel, buttons, labels, widgets):
                if state['phase'] == 'start':
                    app.selected_files[:] = sources
                    buttons['Process PDFs'].invoke()
                    state['phase'] = 'first'
                elif state['phase'] == 'first':
                    self.assertEqual(panel.rows[0]['status'], 'Renamed')
                    app.API_KEY = ''
                    app.OUTPUT_PATTERN = '{title} - cached.pdf'
                    buttons['Process PDFs'].invoke()
                    state['phase'] = 'second'
                elif state['phase'] == 'second':
                    self.assertEqual(metadata.call_count, 1)
                    self.assertEqual(client.call_count, 1)
                    self.assertEqual(panel.rows[0]['extraction_source'], 'cache')
                    self.assertEqual(Path(panel.rows[0]['source']).name, 'Cached title - cached.pdf')
                    self.assertIn('Renamed: 1 / 1 files  ·  Analyzed: 1 / 1', labels)
                    panel.undo_btn.invoke()
                    state['phase'] = 'undo'
                elif state['phase'] == 'undo':
                    self.assertEqual(Path(panel.rows[0]['source']).name, 'Cached title.pdf')
                    state['phase'] = 'complete'
                    return True

            dialogs = self.run_retry_window(directory, advance, metadata, client)
            self.assertFalse(dialogs.called, dialogs.call_args)

    def test_force_refresh_bypasses_cache_in_main_window(self):
        metadata = Mock(side_effect=[{'authors': ['Ada'], 'year': '1843', 'title': title}
                                     for title in ('Original', 'Fresh')])
        client = Mock(return_value=Mock())
        with tempfile.TemporaryDirectory() as directory:
            sources = self.make_sources(directory, 1)
            def advance(state, panel, buttons, labels, widgets):
                if state['phase'] == 'start':
                    app.selected_files[:] = sources
                    buttons['Process PDFs'].invoke()
                    state['phase'] = 'first'
                elif state['phase'] == 'first':
                    force = next(w for w in widgets if isinstance(w, ttk.Checkbutton)
                                 and w.cget('text') == 'Force fresh extraction (ignore cache)')
                    force.invoke()
                    buttons['Process PDFs'].invoke()
                    state['phase'] = 'fresh'
                elif state['phase'] == 'fresh':
                    self.assertEqual(metadata.call_count, 2)
                    self.assertEqual(client.call_count, 2)
                    self.assertEqual(panel.rows[0]['extraction_source'], 'model')
                    self.assertEqual(Path(panel.rows[0]['source']).name, 'Fresh.pdf')
                    state['phase'] = 'complete'
                    return True
            dialogs = self.run_retry_window(directory, advance, metadata, client)
            self.assertFalse(dialogs.called, dialogs.call_args)

    def test_restore_checks_changed_and_missing_sources_before_continuing(self):
        meta = {'authors': ['Ada'], 'year': '2024', 'title': 'Old title'}
        with tempfile.TemporaryDirectory() as directory:
            sources = self.make_sources(directory, 2)
            store = BatchStore(Path(directory) / 'batch-state')
            rows = store.start(sources, {'pages': 4, 'is_book': False,
                               'naming': {'pattern': '{title}.pdf', 'author_format': '{surname}', 'unpublished': 'Unknown'}})
            with patch.object(app, 'get_metadata_from_snippet', return_value=meta):
                app.extract_rows(rows, 4, False, Mock(), 'fake', lambda: False, lambda *args: store.save())
            # Replace one source with different valid PDF bytes, move the other externally.
            Path(sources[0]).write_bytes(Path(sources[1]).read_bytes())
            moved = Path(directory) / 'Moved externally.pdf'
            Path(sources[1]).rename(moved)
            extraction = Mock(return_value=dict(meta, title='Fresh title'))

            def advance(state, panel, buttons, labels, widgets):
                if state['phase'] == 'start':
                    self.assertEqual([r['status'] for r in panel.rows], ['Changed', 'Missing'])
                    extraction.assert_not_called()
                    panel.continue_btn.invoke()
                    state['phase'] = 'continued'
                elif state['phase'] == 'continued':
                    self.assertEqual([r['status'] for r in panel.rows], ['Renamed', 'Missing'])
                    self.assertEqual(Path(panel.rows[0]['source']).name, 'Fresh title.pdf')
                    self.assertEqual(extraction.call_count, 1)
                    self.assertTrue(moved.exists())
                    self.assertFalse((Path(directory) / 'Old title.pdf').exists())
                    self.assertIn('Renamed: 1 / 2 files  ·  Analyzed: 2 / 2', labels)
                    state['phase'] = 'complete'
                    return True

            dialogs = self.run_retry_window(directory, advance, extraction, Mock(return_value=Mock()))
            self.assertFalse(dialogs.called, dialogs.call_args)

    def test_full_window_automatic_batch_correction_and_undo(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f'Tk display unavailable: {exc}')
        root.withdraw()
        errors, phases, snapshots = [], [], []
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'OriginalCase.PDF'
            writer = app.PdfWriter()
            for _ in range(4):
                writer.add_blank_page(width=100, height=100)
            with source.open('wb') as stream:
                writer.write(stream)

            def widgets(parent):
                for child in parent.winfo_children():
                    yield child
                    yield from widgets(child)

            def advance():
                try:
                    panel = next(w for w in widgets(root) if isinstance(w, ResultsPanel))
                    process = next(w for w in widgets(root) if isinstance(w, ttk.Button) and w.cget('text') == 'Process PDFs')
                    if not phases:
                        app.selected_files[:] = [str(source)]
                        process.invoke()
                        phases.append('processing')
                    elif panel.busy:
                        pass
                    elif phases[-1] == 'processing' and panel.rows and panel.rows[0]['status'] == 'Renamed':
                        row = panel.rows[0]
                        snapshots.append(row['snippet_path'])
                        self.assertTrue(Path(row['snippet_path']).exists())
                        self.assertEqual(row['page_count'], 4)
                        self.assertEqual(app.selected_files, [row['source']])
                        self.assertTrue(any(isinstance(w, ttk.Label) and 'Renamed: 1 / 1 files  ·  Analyzed: 1 / 1' == w.cget('text') for w in widgets(root)))
                        panel.apply_correction(row, dict(row['metadata'], title='Corrected'), '')
                        phases.append('correcting')
                    elif phases[-1] == 'correcting':
                        self.assertIn('Corrected', app.selected_files[0])
                        panel.undo_btn.invoke()
                        phases.append('undo_correction')
                    elif phases[-1] == 'undo_correction':
                        self.assertNotIn('Corrected', app.selected_files[0])
                        panel.undo_btn.invoke()
                        phases.append('undo_batch')
                    elif phases[-1] == 'undo_batch':
                        self.assertEqual(app.selected_files, [str(source)])
                        self.assertTrue(source.exists())
                        phases.append('complete')
                        destroy_test_root(root)
                        return
                    root.after(20, advance)
                except Exception as exc:
                    errors.append(exc)
                    destroy_test_root(root)

            def timeout():
                errors.append(AssertionError(f'Window workflow timed out at {phases}'))
                destroy_test_root(root)

            root.after(20, advance)
            root.after(10000, timeout)
            with patch.object(app.tk, 'Tk', return_value=root), patch.object(app, 'FIRST_RUN', False), \
                 patch.object(app, 'STORE_DIR', directory), patch.object(app, 'API_KEY', 'test-only'), \
                 patch.object(app.genai, 'Client', return_value=Mock()), \
                 patch.object(app, 'get_metadata_from_snippet', return_value={'authors': ['Ada Lovelace'], 'year': '1843', 'journal': '', 'title': 'Notes'}), \
                 patch.object(app.messagebox, 'showerror') as error_dialog:
                app.main()
                self.assertFalse(error_dialog.called, error_dialog.call_args)
        self.assertFalse(errors, errors)
        self.assertEqual(phases[-1], 'complete')
        self.assertTrue(snapshots)
        self.assertTrue(all(not Path(path).exists() for path in snapshots))


if __name__ == '__main__':
    unittest.main()
