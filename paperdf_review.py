"""On-demand review of the exact PDF pages used for metadata extraction."""
from copy import deepcopy
from dataclasses import dataclass
import math
import os
import queue
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk


# PDFium is not thread-safe, including document creation and destruction.
_PDFIUM_LOCK = threading.Lock()
_FIELDS = ('authors', 'year', 'journal', 'title')


def _evidence_items(metadata, page_count):
    """Keep source references that can actually be opened in the analyzed PDF."""
    evidence = metadata.get('evidence', {})
    if not isinstance(evidence, dict):
        return []
    items = []
    for field in _FIELDS:
        refs = evidence.get(field, [])
        if not isinstance(refs, list):
            continue
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            page, quote = ref.get('page'), ref.get('quote')
            if (type(page) is int and 1 <= page <= page_count
                    and isinstance(quote, str) and quote.strip()):
                items.append((field, page, quote))
    return items


def _render_page(pdf_bytes, page_number, zoom, viewport_width):
    """Return a detached Pillow image; all native PDF resources close here."""
    import pypdfium2 as pdfium

    with _PDFIUM_LOCK:
        document = page = bitmap = None
        try:
            document = pdfium.PdfDocument(pdf_bytes)
            count = len(document)
            if count < 1:
                raise ValueError('The analyzed PDF contains no pages.')
            number = min(max(1, page_number), count)
            page = document[number - 1]
            width, height = page.get_size()
            if width <= 0 or height <= 0 or not all(map(math.isfinite, (width, height))):
                raise ValueError('This page has an invalid size.')
            scale = (max(200, viewport_width) / width if zoom == 'Fit width'
                     else float(zoom.rstrip('%')) / 100 * 96 / 72)
            # Bound memory use for unusually large page dimensions.
            scale = min(scale, 8192 / max(width, height), math.sqrt(16_000_000 / (width * height)))
            bitmap = page.render(scale=scale)
            image = bitmap.to_pil().copy()
            return image, count, number
        finally:
            if bitmap is not None:
                bitmap.close()
            if page is not None:
                page.close()
            if document is not None:
                document.close()


@dataclass(frozen=True)
class _RenderRequest:
    token: int
    page: int
    zoom: str
    width: int


class _PdfRenderer:
    """One daemon worker per viewer, with only the newest queued request kept."""
    def __init__(self, snippet_path):
        self.path = snippet_path
        self.requests = queue.Queue(maxsize=1)
        self.results = queue.Queue()
        self.cancelled = threading.Event()
        self._state_lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def request(self, request):
        with self._state_lock:
            if self.cancelled.is_set():
                return
            self._replace_request(request)

    def _replace_request(self, request):
        try:
            self.requests.get_nowait()
        except queue.Empty:
            pass
        self.requests.put_nowait(request)

    def _run(self):
        pdf_bytes = None
        while True:
            request = self.requests.get()
            if request is None or self.cancelled.is_set():
                return
            image, count, number, error = None, 0, request.page, ''
            try:
                if pdf_bytes is None:
                    if not self.path:
                        raise FileNotFoundError
                    with open(self.path, 'rb') as source:
                        pdf_bytes = source.read()
                image, count, number = _render_page(pdf_bytes, request.page, request.zoom, request.width)
            except FileNotFoundError:
                error = ('The analyzed pages are no longer available. You can still correct the metadata. '
                         'Process this file again to view its analyzed pages.')
            except ImportError:
                error = 'The PDF viewer is unavailable in this installation. You can still correct the metadata.'
            except Exception:
                error = 'These analyzed pages could not be displayed. You can still correct the metadata.'
            with self._state_lock:
                if self.cancelled.is_set():
                    if image is not None:
                        image.close()
                    return
                self.results.put((request.token, image, count, number, error))

    def close(self):
        with self._state_lock:
            self.cancelled.set()
            self._replace_request(None)
            while True:
                try:
                    _, image, _, _, _ = self.results.get_nowait()
                except queue.Empty:
                    break
                if image is not None:
                    image.close()


class ReviewDialog(tk.Toplevel):
    """Review an extracted row without changing it before the apply callback."""
    def __init__(self, parent, row, build_name, on_apply):
        super().__init__(parent)
        self.withdraw()
        self.title('Review document')
        self.transient(parent.winfo_toplevel())
        self.protocol('WM_DELETE_WINDOW', self.destroy)
        self._metadata = deepcopy(row.get('metadata') or {})
        self._build_name = build_name
        self._on_apply = on_apply
        self._closed = False
        self._applying = False
        self._token = 0
        self._photo = None
        self._poll_id = self._resize_id = None
        self._page = 1
        supplied_count = row.get('page_count', 0)
        self._page_count = supplied_count if type(supplied_count) is int and supplied_count > 0 else 0
        self._rendered_width = 0
        self._is_book = bool(row.get('is_book'))
        self._renderer = _PdfRenderer(row.get('snippet_path'))

        screen_width, screen_height = self.winfo_screenwidth(), self.winfo_screenheight()
        width, height = min(1280, screen_width - 70), min(900, screen_height - 90)
        self.geometry(f'{max(800, width)}x{max(600, height)}')
        self.minsize(min(1000, width), min(680, height))
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        header = ttk.Frame(self, padding=(14, 10, 14, 7))
        header.grid(row=0, column=0, sticky='ew')
        header.columnconfigure(0, weight=1)
        self.filename_label = ttk.Label(header, text=os.path.basename(row.get('source', 'Document')), wraplength=1100)
        self.filename_label.grid(row=0, column=0, sticky='w')
        ttk.Label(header, text='Review the analyzed pages and edit metadata only when needed.').grid(
            row=1, column=0, sticky='w', pady=(4, 0))

        panes = ttk.Panedwindow(self, orient='horizontal')
        panes.grid(row=1, column=0, sticky='nsew', padx=12)
        self._panes = panes
        self._sash_initialized = False
        panes.bind('<Map>', self._place_initial_sash)
        viewer = ttk.Frame(panes)
        editor = ttk.Frame(panes, width=420)
        panes.add(viewer, weight=3)
        panes.add(editor, weight=2)
        viewer.columnconfigure(0, weight=1)
        viewer.rowconfigure(1, weight=1)
        editor.columnconfigure(0, weight=1)
        editor.rowconfigure(1, weight=1)
        self._make_viewer(viewer)
        self._make_editor(editor, row.get('override', ''))

        footer = ttk.Frame(self, padding=12)
        footer.grid(row=2, column=0, sticky='ew')
        ttk.Button(footer, text='Close', command=self.destroy).pack(side='right')
        self.apply_button = ttk.Button(footer, text='Apply correction', command=self._apply)
        self.apply_button.pack(side='right', padx=(0, 8))

        self.bind('<Escape>', lambda event: self.destroy())
        self._refresh_name()
        self._refresh_navigation()
        self._refresh_evidence()
        self.update_idletasks()
        self.deiconify()
        self.grab_set()
        self._request_render()
        self._poll_id = self.after(40, self._poll_render)

    def _place_initial_sash(self, event=None):
        # A withdrawn paned window is only one pixel wide. Setting its sash
        # before it maps can collapse the editor when Tk expands the window.
        if self._closed or self._sash_initialized:
            return
        self._sash_initialized = True
        self._panes.sashpos(0, int(self._panes.winfo_width() * 0.58))

    def _make_viewer(self, parent):
        toolbar = ttk.Frame(parent, padding=(0, 3, 8, 7))
        toolbar.grid(row=0, column=0, columnspan=2, sticky='ew')
        self.previous_button = ttk.Button(toolbar, text='Previous', width=9, command=lambda: self._go_to(self._page - 1))
        self.previous_button.pack(side='left')
        self.next_button = ttk.Button(toolbar, text='Next', width=7, command=lambda: self._go_to(self._page + 1))
        self.next_button.pack(side='left', padx=(4, 12))
        ttk.Label(toolbar, text='Page').pack(side='left')
        self.page_var = tk.StringVar(value='1')
        self.page_entry = ttk.Entry(toolbar, textvariable=self.page_var, width=4)
        self.page_entry.pack(side='left', padx=4)
        self.page_entry.bind('<Return>', self._jump_page)
        self.go_button = ttk.Button(toolbar, text='Go', width=4, command=self._jump_page)
        self.go_button.pack(side='left')
        self.zoom_var = tk.StringVar(value='Fit width')
        zoom = ttk.Combobox(toolbar, textvariable=self.zoom_var, values=('Fit width', '75%', '100%', '125%', '150%', '200%'),
                            state='readonly', width=9)
        zoom.pack(side='right')
        zoom.bind('<<ComboboxSelected>>', lambda event: self._request_render())

        self.canvas = tk.Canvas(parent, background='#e5e7eb', highlightthickness=0)
        self.canvas.grid(row=1, column=0, sticky='nsew')
        vertical = ttk.Scrollbar(parent, orient='vertical', command=self.canvas.yview)
        vertical.grid(row=1, column=1, sticky='ns')
        horizontal = ttk.Scrollbar(parent, orient='horizontal', command=self.canvas.xview)
        horizontal.grid(row=2, column=0, sticky='ew')
        self.canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.canvas.bind('<Configure>', self._resize_viewer)
        self.canvas.bind('<MouseWheel>', self._scroll_page)
        self.canvas.bind('<Button-4>', lambda event: self.canvas.yview_scroll(-3, 'units'))
        self.canvas.bind('<Button-5>', lambda event: self.canvas.yview_scroll(3, 'units'))
        self.canvas.bind('<ButtonPress-1>', lambda event: self.canvas.scan_mark(event.x, event.y))
        self.canvas.bind('<B1-Motion>', lambda event: self.canvas.scan_dragto(event.x, event.y, gain=1))
        self.page_label = tk.StringVar()
        self.viewer_status = tk.StringVar(value='Loading analyzed pages...')
        ttk.Label(parent, textvariable=self.page_label).grid(row=3, column=0, sticky='w', pady=(5, 0))
        self.status_label = ttk.Label(parent, textvariable=self.viewer_status, wraplength=560)
        self.status_label.grid(row=4, column=0, columnspan=2, sticky='w', pady=(2, 4))

    def _make_editor(self, parent, override):
        fields = ttk.LabelFrame(parent, text='Metadata', padding=10)
        fields.grid(row=0, column=0, sticky='new', padx=(10, 0))
        fields.columnconfigure(1, weight=1)
        ttk.Label(fields, text='Authors\n(one per line)').grid(row=0, column=0, sticky='nw', padx=(0, 8))
        self.authors = scrolledtext.ScrolledText(fields, height=3, width=25, wrap='word', undo=True)
        self.authors.grid(row=0, column=1, sticky='ew', pady=(0, 7))
        initial_authors = self._metadata.get('authors', [])
        if isinstance(initial_authors, list):
            self.authors.insert('1.0', '\n'.join(str(author) for author in initial_authors))
        self.authors.bind('<<Modified>>', self._text_changed)
        self.authors.edit_modified(False)
        self.entries = {}
        for index, (field, label) in enumerate((('year', 'Year'), ('journal', 'Publisher' if self._is_book else 'Journal')), 1):
            ttk.Label(fields, text=label).grid(row=index, column=0, sticky='w', padx=(0, 8))
            variable = tk.StringVar(value=self._metadata.get(field) or '')
            variable.trace_add('write', lambda *args: self._refresh_name())
            self.entries[field] = variable
            ttk.Entry(fields, textvariable=variable, width=25).grid(row=index, column=1, sticky='ew', pady=(0, 7))
        ttk.Label(fields, text='Title').grid(row=3, column=0, sticky='nw', padx=(0, 8))
        self.title_entry = scrolledtext.ScrolledText(fields, height=3, width=25, wrap='word', undo=True)
        self.title_entry.grid(row=3, column=1, sticky='ew', pady=(0, 7))
        self.title_entry.insert('1.0', self._metadata.get('title') or '')
        self.title_entry.bind('<<Modified>>', self._text_changed)
        self.title_entry.edit_modified(False)
        ttk.Label(fields, text='Filename override (optional, include .pdf)').grid(row=4, column=0, columnspan=2, sticky='w')
        self.override_var = tk.StringVar(value=override or '')
        self.override_var.trace_add('write', lambda *args: self._refresh_name())
        ttk.Entry(fields, textvariable=self.override_var, width=30).grid(row=5, column=0, columnspan=2, sticky='ew', pady=(3, 7))
        ttk.Label(fields, text='Resulting filename').grid(row=6, column=0, columnspan=2, sticky='w')
        self.proposed_name = tk.StringVar()
        self.proposed_label = ttk.Label(fields, textvariable=self.proposed_name, wraplength=390)
        self.proposed_label.grid(row=7, column=0, columnspan=2, sticky='ew', pady=(3, 0))
        fields.bind('<Configure>', lambda event: self.proposed_label.configure(wraplength=max(220, event.width - 24)))

        sources = ttk.LabelFrame(parent, text='Sources reported during extraction', padding=8)
        sources.grid(row=1, column=0, sticky='nsew', padx=(10, 0), pady=(10, 0))
        sources.columnconfigure(0, weight=1)
        sources.rowconfigure(0, weight=1)
        self.evidence_canvas = tk.Canvas(sources, highlightthickness=0, width=360, height=140)
        self.evidence_canvas.grid(row=0, column=0, sticky='nsew')
        scrollbar = ttk.Scrollbar(sources, orient='vertical', command=self.evidence_canvas.yview)
        scrollbar.grid(row=0, column=1, sticky='ns')
        self.evidence_canvas.configure(yscrollcommand=scrollbar.set)
        self.evidence_frame = ttk.Frame(self.evidence_canvas)
        self._evidence_window = self.evidence_canvas.create_window((0, 0), window=self.evidence_frame, anchor='nw')
        self.evidence_frame.columnconfigure(0, weight=1)
        self._quote_labels = []
        self.evidence_frame.bind('<Configure>', lambda event: self.evidence_canvas.configure(scrollregion=self.evidence_canvas.bbox('all')))
        self.evidence_canvas.bind('<Configure>', self._resize_evidence)

    def _text_changed(self, event):
        if event.widget.edit_modified():
            event.widget.edit_modified(False)
            self._refresh_name()

    def _collect_metadata(self):
        metadata = deepcopy(self._metadata)
        metadata.update(authors=[name.strip() for name in self.authors.get('1.0', 'end-1c').splitlines() if name.strip()],
                        year=self.entries['year'].get().strip() or 'n.d.',
                        journal=self.entries['journal'].get().strip(),
                        title=self.title_entry.get('1.0', 'end-1c').strip())
        return metadata

    def _refresh_name(self):
        if not hasattr(self, 'proposed_name') or self._closed:
            return
        try:
            name = self.override_var.get().strip() or self._build_name(self._collect_metadata())
            self.proposed_name.set(name)
        except Exception as exc:
            self.proposed_name.set(f'Complete the metadata to build a filename: {exc}')

    def _refresh_evidence(self):
        for widget in self.evidence_frame.winfo_children():
            widget.destroy()
        self._quote_labels = []
        items = _evidence_items(self._metadata, self._page_count)
        labels = {'authors': 'Authors', 'year': 'Year', 'journal': 'Publisher' if self._is_book else 'Journal', 'title': 'Title'}
        if not items:
            label = ttk.Label(self.evidence_frame, text='No source passages are available. Browse any of the analyzed pages to check the metadata.', wraplength=340)
            label.grid(row=0, column=0, sticky='ew', pady=5)
            self._quote_labels.append(label)
        for index, (field, page, quote) in enumerate(items):
            line = ttk.Frame(self.evidence_frame)
            line.grid(row=index * 2, column=0, sticky='ew', pady=(3, 0))
            ttk.Label(line, text=labels[field]).pack(side='left')
            ttk.Button(line, text=f'PDF page {page}', command=lambda number=page: self._go_to(number)).pack(side='right')
            label = ttk.Label(self.evidence_frame, text=quote, wraplength=340, justify='left')
            label.grid(row=index * 2 + 1, column=0, sticky='ew', pady=(3, 10))
            self._quote_labels.append(label)
        self._resize_evidence()

    def _resize_evidence(self, event=None):
        width = event.width if event is not None else self.evidence_canvas.winfo_width()
        width = max(100, width)
        self.evidence_canvas.itemconfigure(self._evidence_window, width=width)
        for label in self._quote_labels:
            label.configure(wraplength=max(80, width - 10))

    def _refresh_navigation(self):
        self.page_var.set(str(self._page))
        self.page_label.set(f'PDF page {self._page} of {self._page_count}' if self._page_count else 'Analyzed PDF pages')
        self.previous_button.configure(state='normal' if self._page > 1 else 'disabled')
        self.next_button.configure(state='normal' if self._page < self._page_count else 'disabled')
        self.go_button.configure(state='normal' if self._page_count else 'disabled')

    def _jump_page(self, event=None):
        try:
            number = int(self.page_var.get())
        except ValueError:
            number = 0
        if not self._go_to(number):
            self.page_var.set(str(self._page))
            if self._page_count:
                self.viewer_status.set(f'Enter a PDF page from 1 to {self._page_count}.')
        return 'break'

    def _go_to(self, number):
        if self._closed or type(number) is not int or not 1 <= number <= self._page_count:
            return False
        self._page = number
        self._refresh_navigation()
        self._request_render()
        return True

    def _resize_viewer(self, event):
        if self._closed:
            return
        self.status_label.configure(wraplength=max(200, event.width))
        self.filename_label.configure(wraplength=max(400, self.winfo_width() - 40))
        if self.zoom_var.get() == 'Fit width' and abs(event.width - self._rendered_width) > 15:
            if self._resize_id is not None:
                self.after_cancel(self._resize_id)
            self._resize_id = self.after(180, self._request_render)

    def _scroll_page(self, event):
        if event.delta:
            amount = -max(1, int(abs(event.delta) / 120)) if event.delta > 0 else max(1, int(abs(event.delta) / 120))
            if event.state & 0x0001:
                self.canvas.xview_scroll(amount * 3, 'units')
            else:
                self.canvas.yview_scroll(amount * 3, 'units')
        return 'break'

    def _request_render(self):
        if self._closed:
            return
        if self._resize_id is not None:
            self.after_cancel(self._resize_id)
            self._resize_id = None
        self._token += 1
        self._rendered_width = self.canvas.winfo_width()
        self.viewer_status.set('Loading page...')
        # Remove the preceding page so its image never appears under a new page label.
        self.canvas.delete('all')
        self._photo = None
        self.canvas.configure(scrollregion=(0, 0, 0, 0))
        self._renderer.request(_RenderRequest(self._token, self._page, self.zoom_var.get(), max(200, self._rendered_width - 24)))

    def _poll_render(self):
        self._poll_id = None
        if self._closed:
            return
        while True:
            try:
                token, image, count, number, error = self._renderer.results.get_nowait()
            except queue.Empty:
                break
            if token != self._token:
                if image is not None:
                    image.close()
                continue
            if error:
                self.viewer_status.set(error)
                self.canvas.delete('all')
                self._photo = None
                continue
            try:
                from PIL import ImageTk
                self._photo = ImageTk.PhotoImage(image, master=self)
                self.canvas.delete('all')
                self.canvas.create_image(12, 12, image=self._photo, anchor='nw')
                self.canvas.configure(scrollregion=(0, 0, image.width + 24, image.height + 24))
                self.canvas.xview_moveto(0)
                self.canvas.yview_moveto(0)
                changed_count = count != self._page_count
                self._page_count, self._page = count, number
                self._refresh_navigation()
                if changed_count:
                    self._refresh_evidence()
                self.viewer_status.set('These are the exact pages used for extraction. Scroll or zoom to read.')
            except Exception:
                self.viewer_status.set('This page could not be displayed. You can still correct the metadata.')
            finally:
                if image is not None:
                    image.close()
        self._poll_id = self.after(40, self._poll_render)

    def _apply(self):
        if self._applying or self._closed:
            return
        self._applying = True
        self.apply_button.configure(state='disabled')
        try:
            accepted = self._on_apply(self._collect_metadata(), self.override_var.get().strip())
        except Exception as exc:
            messagebox.showerror('Correction could not be applied', str(exc), parent=self)
            accepted = False
        if accepted is False:
            self._applying = False
            if not self._closed:
                self.apply_button.configure(state='normal')
        else:
            self.destroy()

    def destroy(self):
        if getattr(self, '_closed', True):
            return
        self._closed = True
        for timer in (self._poll_id, self._resize_id):
            if timer is not None:
                try:
                    self.after_cancel(timer)
                except tk.TclError:
                    pass
        self._renderer.close()
        self._photo = None
        try:
            self.grab_release()
        except tk.TclError:
            pass
        super().destroy()
