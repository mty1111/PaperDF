"""Local author-part overrides for the document review dialog."""
from copy import deepcopy
import tkinter as tk
from tkinter import ttk
from paperdf_academic import AUTHOR_KINDS, compact


def aligned_details(authors, details):
    return [deepcopy(next((item for item in details if compact(item['literal']) == compact(name)),
                         {'literal': name, 'kind': 'unknown', 'given': '', 'family': '', 'suffix': ''}))
            for name in authors]


class AuthorDetailsDialog(tk.Toplevel):
    def __init__(self, parent, authors, details, on_apply):
        super().__init__(parent)
        self.withdraw()
        self.title('Author names')
        self.transient(parent)
        self.resizable(True, False)
        self.columnconfigure(1, weight=1)
        self.details = aligned_details(authors, details)
        self.on_apply = on_apply
        self.current = None
        ttk.Label(self, text='Author').grid(row=0, column=0, padx=10, pady=8)
        self.selector = ttk.Combobox(self, values=authors, state='readonly', width=50)
        self.selector.grid(row=0, column=1, padx=10, pady=8, sticky='ew')
        self.selector.bind('<<ComboboxSelected>>', lambda event: self.select_author())
        self.variables = {}
        for row, (key, label) in enumerate((('kind', 'Type'), ('given', 'Given names'),
                                           ('family', 'Full family name'), ('suffix', 'Suffix')), 1):
            ttk.Label(self, text=label).grid(row=row, column=0, padx=10, pady=5, sticky='e')
            variable = tk.StringVar()
            self.variables[key] = variable
            widget = (ttk.Combobox(self, textvariable=variable, values=AUTHOR_KINDS, state='readonly')
                      if key == 'kind' else ttk.Entry(self, textvariable=variable))
            widget.grid(row=row, column=1, padx=10, pady=5, sticky='ew')
        ttk.Label(self, text='Keep compound surnames together. Organizations and unknown names use the full literal name.\n'
                  'These edits stay local until Apply correction in the document window.', wraplength=500).grid(
                      row=5, column=0, columnspan=2, padx=10, pady=10)
        self.save_button = ttk.Button(self, text='Use these names', command=self.save)
        self.save_button.grid(row=6, column=1, padx=10, pady=10, sticky='e')
        self.bind('<Escape>', lambda event: self.destroy())
        if authors:
            self.selector.current(0)
            self.select_author()
        self.deiconify()
        self.grab_set()

    def store_current(self):
        if self.current is not None:
            item = self.details[self.current]
            item.update({key: compact(var.get()) for key, var in self.variables.items()})
            if item['kind'] != 'person':
                item.update(given='', family='', suffix='')

    def select_author(self):
        self.store_current()
        self.current = self.selector.current()
        if self.current >= 0:
            for key, var in self.variables.items():
                var.set(self.details[self.current][key])

    def save(self):
        self.store_current()
        self.on_apply(deepcopy(self.details))
        self.destroy()

    def destroy(self):
        parent = self.master
        super().destroy()
        # Return modality to the owning document window.
        if parent.winfo_exists() and parent.winfo_viewable():
            parent.grab_set()
