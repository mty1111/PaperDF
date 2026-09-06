"""Offline multipage rendering, navigation, and correction tests."""
from copy import deepcopy
from io import BytesIO
from pathlib import Path
import queue
import tempfile
import threading
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import Mock, patch

from PyPDF2 import PdfWriter
from PyPDF2.generic import DecodedStreamObject, NameObject

from paperdf_review import ReviewDialog, _evidence_items, _PdfRenderer, _RenderRequest, _render_page
from paperdf_author_editor import AuthorDetailsDialog


def sample_pdf():
    writer = PdfWriter()
    for color in ('1 0 0', '0 1 0', '0 0 1'):
        writer.add_blank_page(width=300, height=400)
        page = writer.pages[-1]
        content = DecodedStreamObject()
        content.set_data(f'q {color} rg 0 0 300 400 re f Q'.encode('ascii'))
        page[NameObject('/Contents')] = writer._add_object(content)
    stream = BytesIO()
    writer.write(stream)
    return stream.getvalue()


class PdfRenderingTests(unittest.TestCase):
    def test_actual_multipage_content_and_page_count(self):
        data = sample_pdf()
        for page, rgb in ((1, (255, 0, 0)), (2, (0, 255, 0)), (3, (0, 0, 255))):
            image, count, rendered_page = _render_page(data, page, 'Fit width', 300)
            try:
                self.assertEqual((count, rendered_page), (3, page))
                self.assertEqual(image.size, (300, 400))
                self.assertEqual(image.getpixel((150, 200))[:3], rgb)
            finally:
                image.close()

    def test_renderer_uses_snapshot_after_input_file_is_renamed(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'garbled.pdf'
            snippet = Path(directory) / 'analyzed.pdf'
            source.write_bytes(sample_pdf())
            snippet.write_bytes(source.read_bytes())
            source.rename(source.with_name('Recognized title.pdf'))
            renderer = _PdfRenderer(str(snippet))
            try:
                renderer.request(_RenderRequest(1, 2, 'Fit width', 300))
                token, image, count, page, error = renderer.results.get(timeout=10)
                try:
                    self.assertEqual((token, count, page, error), (1, 3, 2, ''))
                    self.assertEqual(image.getpixel((150, 200))[:3], (0, 255, 0))
                finally:
                    image.close()
            finally:
                renderer.close()
                renderer.thread.join(timeout=5)
            self.assertFalse(renderer.thread.is_alive())

    def test_native_resources_close_when_rendering_raises(self):
        document, page = Mock(), Mock()
        document.__len__ = Mock(return_value=3)
        document.__getitem__ = Mock(return_value=page)
        page.get_size.return_value = (300, 400)
        page.render.side_effect = RuntimeError('render failed')
        with patch('pypdfium2.PdfDocument', return_value=document):
            with self.assertRaisesRegex(RuntimeError, 'render failed'):
                _render_page(b'fixture', 1, '100%', 300)
        page.close.assert_called_once_with()
        document.close.assert_called_once_with()

    def test_close_discards_image_from_pending_render(self):
        entered, release = threading.Event(), threading.Event()
        rendered_image = Mock()

        def delayed_render(*args):
            entered.set()
            release.wait(timeout=5)
            return rendered_image, 3, 1

        with tempfile.TemporaryDirectory() as directory:
            snippet = Path(directory) / 'analyzed.pdf'
            snippet.write_bytes(sample_pdf())
            with patch('paperdf_review._render_page', side_effect=delayed_render):
                renderer = _PdfRenderer(str(snippet))
                try:
                    renderer.request(_RenderRequest(1, 1, 'Fit width', 300))
                    self.assertTrue(entered.wait(timeout=5))
                    renderer.close()
                finally:
                    release.set()
                    renderer.thread.join(timeout=5)
                self.assertFalse(renderer.thread.is_alive())
                self.assertTrue(renderer.results.empty())
                rendered_image.close.assert_called_once_with()

    def test_evidence_only_exposes_valid_physical_pages_and_exact_quote(self):
        metadata = {'evidence': {
            'authors': [{'page': 2, 'quote': 'Ada\nLovelace'}, {'page': True, 'quote': 'bad'}, {'page': '1', 'quote': 'bad'}],
            'year': [{'page': 0, 'quote': 'bad'}, {'page': 4, 'quote': 'bad'}, {'page': 1, 'quote': ''}],
            'journal': 'bad',
            'title': [{'page': 3, 'quote': '  Exact original passage  '}, None],
        }}
        self.assertEqual(_evidence_items(metadata, 3), [('authors', 2, 'Ada\nLovelace'), ('title', 3, '  Exact original passage  ')])
        self.assertEqual(_evidence_items({'evidence': None}, 3), [])


class ReviewWidgetTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f'Tk display unavailable: {exc}')
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.snippet = Path(self.temp.name) / 'analyzed.pdf'
        self.snippet.write_bytes(sample_pdf())
        self.errors = self.enterContext(patch('paperdf_review.messagebox.showerror'))
        # Keep every integration-test dialog withdrawn, including construction.
        self.enterContext(patch.object(ReviewDialog, 'deiconify'))
        # A withdrawn window cannot own a Cocoa modal grab. Real dialogs are
        # shown before grabbing; the hidden test fixture must omit both actions.
        self.enterContext(patch.object(ReviewDialog, 'grab_set'))
        self.dialogs = []
        self.addCleanup(self._close_dialogs)

    def _close_dialogs(self):
        for dialog in self.dialogs:
            dialog.destroy()
            dialog._renderer.thread.join(timeout=5)

    def row(self):
        return {'source': str(Path(self.temp.name) / 'Already renamed.pdf'), 'original_source': 'garbled.pdf',
                'snippet_path': str(self.snippet), 'page_count': 3, 'finished': True,
                'metadata': {'authors': ['Ada Lovelace'], 'year': '1843', 'journal': 'Scientific Memoirs',
                             'title': 'Notes', 'evidence': {'year': [{'page': 3, 'quote': '1843'}]}}}

    def dialog(self, row=None, apply=None):
        dialog = ReviewDialog(self.root, row or self.row(), lambda meta: meta['title'] + '.pdf', apply or Mock())
        self.dialogs.append(dialog)
        return dialog

    def academic_row(self):
        row = self.row()
        row['metadata'].update(authors=['Gabriel García Márquez', 'World Bank'], year='2021', academic={
            'document_kind': 'preprint', 'author_details': [
                {'literal': 'Gabriel García Márquez', 'kind': 'person', 'given': 'Gabriel García', 'family': 'Márquez', 'suffix': ''},
                {'literal': 'World Bank', 'kind': 'organization', 'given': '', 'family': '', 'suffix': ''}],
            'dates': [{'kind': 'revision', 'year': year, 'page': page, 'quote': f'Revised {year}'}
                      for year, page in [('2021', 2), ('2023', 3)]]})
        return row

    def pump_until(self, predicate):
        # Use the application's event-loop entry point. Cocoa's nested update
        # can keep draining native events without returning to the test deadline.
        completed = []
        errors = []
        poll_id = None

        def poll():
            nonlocal poll_id
            poll_id = None
            try:
                if predicate():
                    completed.append(True)
                    self.root.quit()
                else:
                    poll_id = self.root.after(10, poll)
            except Exception as exc:
                errors.append(exc)
                self.root.quit()

        timeout_id = self.root.after(10000, self.root.quit)
        poll_id = self.root.after(10, poll)
        try:
            self.root.mainloop()
        finally:
            self.root.after_cancel(timeout_id)
            if poll_id is not None:
                self.root.after_cancel(poll_id)
        if errors:
            raise errors[0]
        self.assertTrue(completed, 'PDF rendering or UI update did not complete')

    def test_no_evidence_still_allows_navigation_and_zoom(self):
        row = self.row()
        row['metadata']['evidence'] = {}
        dialog = self.dialog(row)
        self.pump_until(lambda: dialog._photo is not None)
        self.assertEqual(dialog.page_label.get(), 'PDF page 1 of 3')
        dialog.next_button.invoke()
        self.pump_until(lambda: dialog._photo is not None)
        self.assertEqual(dialog._page, 2)
        dialog.page_var.set('3')
        dialog.go_button.invoke()
        self.pump_until(lambda: dialog._photo is not None)
        self.assertEqual(dialog.page_label.get(), 'PDF page 3 of 3')
        self.assertEqual(str(dialog.next_button.cget('state')), 'disabled')
        dialog.zoom_var.set('150%')
        dialog._request_render()
        self.pump_until(lambda: dialog._photo is not None)
        self.assertEqual(dialog._photo.width(), 600)
        dialog.page_var.set('4')
        dialog._jump_page()
        self.assertEqual(dialog._page, 3)
        self.assertIn('1 to 3', dialog.viewer_status.get())

    def test_mapped_editor_stays_visible_at_default_and_minimum_sizes(self):
        # Mapping is essential: a withdrawn ttk.Panedwindow can report its
        # requested size even though its editor will collapse when shown.
        # Override-redirect windows live entirely offscreen and do not grab focus.
        self.root.overrideredirect(True)
        self.root.geometry('10x10+20000+20000')
        self.root.deiconify()
        self.pump_until(lambda: self.root.winfo_ismapped())
        with patch.object(ReviewDialog, 'grab_set'):
            dialog = self.dialog(self.academic_row())
        dialog.overrideredirect(True)
        dialog.geometry('1280x860+20000+20000')
        tk.Toplevel.deiconify(dialog)
        for width, height in ((1280, 860), (1000, 680)):
            dialog.geometry(f'{width}x{height}+20000+20000')

            def layout_ready():
                if dialog.winfo_width() != width or dialog.winfo_height() != height:
                    return False
                for widget in (dialog.authors, dialog.title_entry, dialog.evidence_canvas, dialog.apply_button, dialog.year_choice):
                    x = widget.winfo_rootx() - dialog.winfo_rootx()
                    y = widget.winfo_rooty() - dialog.winfo_rooty()
                    if (not widget.winfo_ismapped() or widget.winfo_width() < 80 or widget.winfo_height() < 20
                            or x < 0 or y < 0 or x + widget.winfo_width() > width
                            or y + widget.winfo_height() > height):
                        return False
                return True

            self.pump_until(layout_ready)
            self.assertGreater(dialog.canvas.winfo_width(), 400)
            self.assertGreater(dialog.evidence_canvas.winfo_height(), 100)

    def test_name_parts_and_version_year_edits_stay_local_until_apply(self):
        row = self.academic_row()
        before = deepcopy(row)
        callback = Mock()
        dialog = self.dialog(row, callback)
        with patch.object(AuthorDetailsDialog, 'deiconify'), patch.object(AuthorDetailsDialog, 'grab_set'):
            editor = dialog.edit_author_details()
        try:
            editor.variables['given'].set('Gabriel')
            editor.variables['family'].set('García Márquez')
            editor.selector.current(1)
            editor.select_author()
            editor.variables['family'].set('Bank')  # organizations discard personal parts
            editor.save_button.invoke()
        finally:
            if editor.winfo_exists():
                editor.destroy()
        dialog.year_choice.current(1)
        dialog.choose_year()
        self.pump_until(lambda: dialog._photo is not None)
        self.assertEqual(dialog._page, 3)
        self.assertEqual(dialog.entries['year'].get(), '2023')
        self.assertEqual(row, before)
        self.assertFalse(callback.called)
        dialog.apply_button.invoke()
        metadata = callback.call_args.args[0]
        self.assertEqual(metadata['academic']['author_details'][0]['family'], 'García Márquez')
        self.assertEqual(metadata['academic']['author_details'][1]['family'], '')
        self.assertEqual(metadata['academic']['author_details'][1]['kind'], 'organization')
        self.assertEqual(metadata['year'], '2023')
        self.assertEqual(metadata['academic']['dates'], before['metadata']['academic']['dates'])

    def test_cancelled_name_parts_editor_does_not_change_metadata(self):
        dialog = self.dialog(self.academic_row())
        before = deepcopy(dialog._metadata)
        with patch.object(AuthorDetailsDialog, 'deiconify'), patch.object(AuthorDetailsDialog, 'grab_set'):
            editor = dialog.edit_author_details()
        editor.variables['family'].set('Unwanted edit')
        editor.destroy()
        self.assertEqual(dialog._metadata, before)

    def test_evidence_button_opens_page_three_in_renamed_row(self):
        dialog = self.dialog()
        self.pump_until(lambda: dialog._photo is not None)
        line = next(widget for widget in dialog.evidence_frame.winfo_children() if isinstance(widget, ttk.Frame))
        button = next(widget for widget in line.winfo_children() if isinstance(widget, ttk.Button))
        self.assertEqual(button.cget('text'), 'PDF page 3')
        button.invoke()
        self.pump_until(lambda: dialog._photo is not None)
        self.assertEqual(dialog._page, 3)
        self.assertEqual(dialog.page_label.get(), 'PDF page 3 of 3')

    def test_corrections_stay_local_until_callback_and_preserve_evidence(self):
        row = self.row()
        original = deepcopy(row)
        callback = Mock()
        dialog = self.dialog(row, callback)
        dialog.title_entry.delete('1.0', 'end')
        dialog.title_entry.insert('1.0', 'Corrected notes')
        self.pump_until(lambda: dialog.proposed_name.get() == 'Corrected notes.pdf')
        self.assertEqual(dialog.proposed_name.get(), 'Corrected notes.pdf')
        self.assertEqual(row, original)
        dialog.apply_button.invoke()
        metadata, override = callback.call_args.args
        self.assertEqual(metadata['title'], 'Corrected notes')
        self.assertEqual(metadata['evidence'], original['metadata']['evidence'])
        self.assertEqual(override, '')
        self.assertEqual(row, original)
        self.assertTrue(dialog._closed)

    def test_callback_rejection_keeps_edits_and_reenables_apply(self):
        callback = Mock(return_value=False)
        row = self.row()
        original = deepcopy(row)
        dialog = self.dialog(row, callback)
        dialog.override_var.set('manual.pdf')
        dialog.apply_button.invoke()
        self.assertFalse(dialog._closed)
        self.assertEqual(str(dialog.apply_button.cget('state')), 'normal')
        self.assertEqual(dialog.override_var.get(), 'manual.pdf')
        self.assertEqual(row, original)

    def test_missing_snippet_keeps_metadata_editing_available(self):
        row = self.row()
        row['snippet_path'] = str(Path(self.temp.name) / 'missing.pdf')
        dialog = self.dialog(row)
        self.pump_until(lambda: 'no longer available' in dialog.viewer_status.get())
        self.assertIsNone(dialog._photo)
        self.assertEqual(str(dialog.apply_button.cget('state')), 'normal')
        self.assertFalse(self.errors.called)

    def test_rapid_page_requests_discard_stale_render_results(self):
        dialog = self.dialog()
        dialog._go_to(2)
        dialog._go_to(3)
        self.pump_until(lambda: dialog._photo is not None)
        self.assertEqual(dialog.page_label.get(), 'PDF page 3 of 3')
        self.assertEqual(dialog._photo._PhotoImage__photo.get(100, 100), (0, 0, 255))


if __name__ == '__main__':
    unittest.main()
