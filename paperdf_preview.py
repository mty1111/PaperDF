"""Batch results and on-demand document review."""
import copy
import os
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from paperdf_processing import can_retry_extraction, correct_row, process_rows, record_results
from paperdf_review import ReviewDialog
from paperdf_session import can_continue, remember_move
from paperdf_workflow import undo_last_batch


class ResultsPanel(ttk.Frame):
    def __init__(self, parent, journal_dir, dispatch, set_busy, log, stop_event, on_paths_changed=None,
                 on_counts_changed=None, on_retry=None, on_continue=None, checkpoint=None):
        super().__init__(parent)
        self.journal_dir = journal_dir
        self.dispatch = dispatch
        self.set_busy = set_busy
        self.log = log
        self.stop_event = stop_event
        self.on_paths_changed = on_paths_changed or (lambda results: None)
        self.on_counts_changed = on_counts_changed or (lambda renamed, total: None)
        self.on_retry = on_retry
        self.on_continue = on_continue
        self.checkpoint = checkpoint or (lambda: None)
        self.total_files = 0
        self.rows = []
        self.busy = False
        self.build_name = None
        self.is_book = False
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        heading = ttk.Frame(self)
        heading.grid(row=0, column=0, sticky='ew', pady=(0, 8))
        heading.columnconfigure(0, weight=1)
        self.summary = tk.StringVar(value='Process PDFs to see results here.')
        ttk.Label(heading, textvariable=self.summary).grid(row=0, column=0, sticky='w')
        self.attention_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(heading, text='Needs attention only', variable=self.attention_only,
                        command=self._render).grid(row=0, column=1, sticky='e')
        self.tree = ttk.Treeview(self, columns=('title', 'authors', 'year', 'journal', 'status'),
                                 show='headings', selectmode='browse', height=10)
        for key, title, width in [('title', 'Title', 380), ('authors', 'Authors', 220),
                                  ('year', 'Year', 60), ('journal', 'Journal / Publisher', 200),
                                  ('status', 'Result', 120)]:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=50, stretch=key != 'year')
        self.tree.grid(row=1, column=0, sticky='nsew')
        scrollbar = ttk.Scrollbar(self, orient='vertical', command=self.tree.yview)
        scrollbar.grid(row=1, column=1, sticky='ns')
        horizontal = ttk.Scrollbar(self, orient='horizontal', command=self.tree.xview)
        horizontal.grid(row=2, column=0, sticky='ew')
        self.tree.configure(yscrollcommand=scrollbar.set, xscrollcommand=horizontal.set)
        self.tree.tag_configure('attention', foreground='#9a3412')
        self.tree.bind('<Double-1>', lambda event: self.review_selected())
        self.tree.bind('<Return>', lambda event: self.review_selected())
        self.tree.bind('<<TreeviewSelect>>', lambda event: self._details())
        self.details = tk.StringVar(value='Double-click a result to review its analyzed pages and metadata.')
        ttk.Label(self, textvariable=self.details, wraplength=950).grid(row=3, column=0, sticky='w', pady=8)
        buttons = ttk.Frame(self)
        buttons.grid(row=4, column=0, sticky='ew')
        self.review_btn = ttk.Button(buttons, text='Review document...', command=self.review_selected)
        self.review_btn.pack(side='left', padx=(0, 8))
        self.retry_btn = ttk.Button(buttons, text='Retry failed files', command=self.retry_failed)
        self.retry_btn.pack(side='left', padx=8)
        self.continue_btn = ttk.Button(buttons, text='Continue batch', command=self.continue_batch)
        self.continue_btn.pack(side='left', padx=8)
        self.undo_btn = ttk.Button(buttons, text='Undo last batch', command=self.undo)
        self.undo_btn.pack(side='left', padx=8)
        self.set_enabled(True)

    def set_enabled(self, enabled):
        self.busy = not enabled
        self.review_btn.configure(state='normal' if enabled and self.rows else 'disabled')
        self.retry_btn.configure(state='normal' if enabled and self.on_retry and
                                 any(can_retry_extraction(row) for row in self.rows) else 'disabled')
        self.undo_btn.configure(state='normal' if enabled else 'disabled')
        self.continue_btn.configure(state='normal' if enabled and self.on_continue and
                                    any(can_continue(row) for row in self.rows) else 'disabled')

    def continue_batch(self):
        if not self.busy and self.on_continue and any(can_continue(row) for row in self.rows):
            self.on_continue()

    def retry_failed(self):
        if not self.busy and self.on_retry and any(can_retry_extraction(row) for row in self.rows):
            self.on_retry()

    def load(self, rows, build_name, is_book, automatic=True, total_files=None, mark_stopped=True):
        self.rows = rows
        self.total_files = len(rows) if total_files is None else total_files
        self.build_name = build_name
        self.is_book = is_book
        self._render()
        if automatic and not self.stop_event.is_set():
            self._work(lambda: process_rows(self.rows, self.build_name, self.journal_dir,
                                            self.stop_event.is_set, self._report, self._before_rename), self._completed)
        else:
            for row in rows:
                if mark_stopped and not row.get('needs_review'):
                    row.update(status='Stopped', needs_review=True, resume_action='rename',
                               message='Stopped before renaming. Continue batch or review this document to apply locally.')
            self._render()
            self.set_enabled(True)

    def _render(self):
        focus = self.tree.focus()
        self.tree.delete(*self.tree.get_children())
        order = sorted(range(len(self.rows)), key=lambda i: not self.rows[i].get('needs_review', False))
        for index in order:
            row = self.rows[index]
            if self.attention_only.get() and not row.get('needs_review'):
                continue
            meta = row.get('metadata', {})
            self.tree.insert('', 'end', iid=str(index), values=(
                meta.get('title') or '(Title unavailable)', ', '.join(meta.get('authors', [])),
                meta.get('year', ''), meta.get('journal', ''), row.get('status', '')),
                tags=('attention',) if row.get('needs_review') else ())
        children = self.tree.get_children()
        if focus and self.tree.exists(focus):
            selected = focus
        else:
            selected = children[0] if children else None
        if selected:
            self.tree.focus(selected)
            self.tree.selection_set(selected)
        self._update_counts()
        self._details()

    def _renamed_count(self):
        # Count documents whose names are currently changed, not rename operations.
        # A second correction or undoing only that correction still counts once.
        return sum(os.path.normcase(os.path.abspath(row['source'])) !=
                   os.path.normcase(os.path.abspath(row.get('original_source', row['source'])))
                   for row in self.rows)

    def _update_counts(self, renamed=None):
        if renamed is None:
            renamed = self._renamed_count()
        attention = sum(bool(row.get('needs_review')) for row in self.rows)
        total = self.total_files if self.total_files is not None else '?'
        self.summary.set(f'Renamed: {renamed} / {total} files · {attention} need attention')
        self.on_counts_changed(renamed, self.total_files)

    def _focused_row(self):
        selected = self.tree.selection()
        return self.rows[int(selected[0])] if selected else None

    def _details(self):
        row = self._focused_row()
        if row:
            detail = row.get('message') or row.get('reason') or 'Review the analyzed pages if you want to check or correct the metadata.'
            self.details.set(row['source'] + '\n' + detail)
        else:
            self.details.set('No results match this view.')

    def _work(self, operation, done):
        self.stop_event.clear()
        self.set_busy(True)

        def worker():
            try:
                result = operation()
                self.checkpoint()
            except Exception as exc:
                error = str(exc)
                self.dispatch(lambda: finish(None, error))
            else:
                self.dispatch(lambda: finish(result, None))

        def finish(result, error):
            self.set_busy(False)
            if error:
                self.log(error + '\n')
                messagebox.showerror('Operation incomplete', error, parent=self)
            done(result, error)
            try:
                self.checkpoint()
            except Exception as exc:
                self.stop_event.set()
                self.log(f'Progress not saved: {exc}\n')
                if not error:
                    messagebox.showerror('Progress not saved', str(exc), parent=self)

        threading.Thread(target=worker, daemon=True).start()

    def _report(self, result):
        self.checkpoint()
        renamed = self._renamed_count()
        def update():
            if result.status in ('renamed', 'restored'):
                self.on_paths_changed([result])
            self.log(f'{result.status}: {result.source} → {result.destination} {result.message}\n')
            self._update_counts(renamed)
        self.dispatch(update)

    def _before_rename(self, result):
        remember_move(self.rows, result, 'rename')
        self.checkpoint()

    def _before_undo(self, result):
        remember_move(self.rows, result, 'undo')
        self.checkpoint()

    def _completed(self, result, error):
        if error:
            for row in self.rows:
                if row.get('status') == 'Ready':
                    row.update(status='Needs review', needs_review=True,
                               message='The operation was interrupted. Undo last batch can recover recorded changes.')
        self._render()
        self.set_enabled(True)
        self.log(self.summary.get() + '\n')

    def review_selected(self):
        row = self._focused_row()
        if self.busy or not row:
            return
        review_row = copy.deepcopy(row)
        review_row['is_book'] = self.is_book
        review_row['override'] = row.get('suggested_name') or row.get('override', '')
        return ReviewDialog(self, review_row, self.build_name,
                            lambda metadata, override: self.apply_correction(row, metadata, override))

    def apply_correction(self, row, metadata, override):
        if self.busy:
            return

        def done(result, error):
            if error:
                row.update(status='Failed', needs_review=True, message=error)
            self._completed(result, error)

        self._work(lambda: correct_row(row, metadata, override, self.build_name, self.journal_dir,
                                       self.stop_event.is_set, self._report, self._before_rename), done)

    def undo(self):
        if self.busy:
            return

        def report(result):
            record_results(self.rows, [result])
            self._report(result)

        def done(result, error):
            if result is not None and result.batch_id is None:
                self.log('No batch is available to undo. Current results kept.\n')
            elif result is not None:
                count = sum(r.status == 'restored' for r in result.results)
                self.log(f'Undo restored {count} file(s). Failed entries remain available for retry.\n')
            self._render()
            self.set_enabled(True)

        self._work(lambda: undo_last_batch(self.journal_dir, self.stop_event.is_set, report, self._before_undo), done)
