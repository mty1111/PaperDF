# pdf_metadata_renamer.py — PaperDF edition
# Features:
# - App branding via APP_NAME = "PaperDF"
# - Separate author-format options for papers vs. books
# - Default model: gemini-2.5-flash-lite
# - Default book template: {authors} - {title} - {journal} ({year}).pdf
# - Embedded Cheatsheet inside Settings (no separate menu)
# - Expanded Help with full user guide
# - First-run setup guide popup when no config file exists
# - Automatic batches, on-demand multi-page review, and persistent undo
# - Duplicate-content detection and no-overwrite renaming
# - App/window icon support (dev + PyInstaller onefile)

import os
import sys
import logging
import threading
import tempfile
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk
from tkinter import PhotoImage

from PyPDF2 import PdfReader, PdfWriter

# Google AI Studio SDK (pip install google-genai)
from google import genai
from paperdf_preview import ResultsPanel
from paperdf_processing import can_retry_extraction, process_rows
from paperdf_session import BatchStore, can_continue, validate_rows
from paperdf_workflow import fingerprint_file
from paperdf_cache import ExtractionCache
from paperdf_academic import parse_aliases, DEFAULT_ALIASES

# Branding
APP_NAME = "PaperDF"  # Paper Document Formatter
def _read_version_from_file() -> str:
    try:
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        vfile = os.path.join(base, "VERSION.txt")
        if os.path.exists(vfile):
            with open(vfile, "r", encoding="utf-8") as f:
                v = f.read().strip()
                if v:
                    return v
    except Exception:
        pass
    return ""

APP_VERSION = os.getenv("APP_VERSION", "").strip() or _read_version_from_file() or "dev"
def show_about_dialog(parent=None):
    win = tk.Toplevel(parent) if parent else tk.Toplevel()
    win.title(f'About — {APP_NAME}')
    win.resizable(False, False)
    txt = scrolledtext.ScrolledText(win, width=70, height=16)
    txt.pack(fill='both', expand=True, padx=10, pady=10)
    info = (
        f"{APP_NAME}\n"
        f"Version: {APP_VERSION}\n"
        f"Default Model: {MODEL_NAME}\n"
        f"Config Path: {CONFIG_PATH}\n"
        f"ENV Path: {ENV_PATH}\n"
        f"Store Dir: {STORE_DIR}\n"
        "\n"
        "This tool renames PDFs using structured front-matter metadata.\n"
        "© 2025–2026 PaperDF contributors\n"
    )
    txt.insert('1.0', info)
    txt.config(state='disabled')
    tk.Button(win, text='Close', command=win.destroy).pack(pady=(0,10))
# =========================
# Storage locations
# =========================
ENV_FILENAME = 'pdf_metadata_renamer.env'
CONFIG_FILENAME = 'pdf_metadata_renamer.config'
ENV_SUBDIR = 'pdfrenamer'

from paperdf_config import default_store_dir, load_config, api_key_for
STORE_DIR = str(default_store_dir())
ENV_PATH = os.path.join(STORE_DIR, ENV_FILENAME)
CONFIG_PATH = os.path.join(STORE_DIR, CONFIG_FILENAME)

# =========================
# Defaults & constants
# =========================
API_KEY = os.getenv('GEMINI_API_KEY', '')
from paperdf_defaults import (DEFAULT_MODEL, INVALID_FILENAME_CHARS, DEFAULT_OUTPUT_PATTERN,
                               DEFAULT_BOOK_OUTPUT_PATTERN, DEFAULT_UNPUBLISHED, DEFAULT_PAPER_PAGES,
                               DEFAULT_BOOK_PAGES, MAX_PAGES_TO_EXTRACT, DEFAULT_AUTHOR_FMT_PAPER,
                               DEFAULT_AUTHOR_FMT_BOOK)
MODEL_NAME = DEFAULT_MODEL

# Detect first run (no config file yet)
FIRST_RUN = not os.path.exists(CONFIG_PATH)

# =========================
# Load or initialize config
# =========================
config = load_config(STORE_DIR)
settings = config['Settings']

OUTPUT_PATTERN = settings.get('output_pattern', DEFAULT_OUTPUT_PATTERN)
BOOK_OUTPUT_PATTERN = settings.get('book_output_pattern', DEFAULT_BOOK_OUTPUT_PATTERN)
UNPUBLISHED_PLACEHOLDER = settings.get('unpublished', DEFAULT_UNPUBLISHED)
API_KEY = api_key_for(STORE_DIR, settings)
MODEL_NAME = settings.get('model', MODEL_NAME)

# Separate author formats
AUTHOR_FMT_PAPER = settings.get('author_format_paper', DEFAULT_AUTHOR_FMT_PAPER)
AUTHOR_FMT_BOOK = settings.get('author_format_book', DEFAULT_AUTHOR_FMT_BOOK)
TITLE_STYLE = settings.get('title_style', 'preserve')
JOURNAL_ALIASES = settings.get('journal_aliases', DEFAULT_ALIASES)

# Globals
client = None
stop_event = threading.Event()
selected_files = []
files_entry = None
folder_entry = None

def run_on_ui(root, callback):
    try:
        root.after(0, callback)
    except (RuntimeError, tk.TclError):
        pass

def append_log(root, log_widget, message: str):
    def write():
        log_widget.insert(tk.END, message)
        log_widget.see(tk.END)
    run_on_ui(root, write)

def clear_log(root, log_widget):
    run_on_ui(root, lambda: log_widget.delete('1.0', tk.END))

def set_progress(root, progress_bar, value=None, maximum=None, stop=False):
    def update():
        if maximum is not None:
            progress_bar.config(maximum=maximum)
        if value is not None:
            progress_bar['value'] = value
        if stop:
            progress_bar.stop()
    run_on_ui(root, update)

def show_error(root, title: str, message: str):
    run_on_ui(root, lambda: messagebox.showerror(title, message))

# =========================
# Resource path (for icons/assets, dev + PyInstaller onefile)
# =========================
def resource_path(rel_path: str) -> str:
    """Get absolute path to resource, works for dev and PyInstaller --onefile."""
    base_path = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, rel_path)

# =========================
# Save config
# =========================
def save_config():
    os.makedirs(STORE_DIR, exist_ok=True)
    config['Settings']['output_pattern'] = OUTPUT_PATTERN
    config['Settings']['book_output_pattern'] = BOOK_OUTPUT_PATTERN
    config['Settings']['unpublished'] = UNPUBLISHED_PLACEHOLDER
    config['Settings']['api_key'] = API_KEY
    config['Settings']['model'] = MODEL_NAME
    config['Settings']['author_format_paper'] = AUTHOR_FMT_PAPER
    config['Settings']['author_format_book'] = AUTHOR_FMT_BOOK
    config['Settings']['title_style'] = TITLE_STYLE
    config['Settings']['journal_aliases'] = JOURNAL_ALIASES
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        config.write(f)

# =========================
# File selection
# =========================
def select_files():
    global selected_files, files_entry, folder_entry
    files = filedialog.askopenfilenames(title='Select PDF files', filetypes=[('PDF Files', '*.pdf')])
    selected_files = list(files)
    if selected_files and folder_entry is not None:
        folder_entry.delete(0, tk.END)
    if files_entry is not None:
        files_entry.config(state='normal')
        files_entry.delete(0, tk.END)
        if selected_files:
            files_entry.insert(0, f"{len(selected_files)} files selected")
        files_entry.config(state='readonly')

def browse_folder(entry: tk.Entry):
    global selected_files, files_entry
    path = filedialog.askdirectory(title='Select PDF folder')
    if not path:
        return
    entry.delete(0, tk.END)
    entry.insert(0, path)
    selected_files = []
    if files_entry is not None:
        files_entry.config(state='normal')
        files_entry.delete(0, tk.END)
        files_entry.config(state='readonly')

# =========================
# Settings, Help
# =========================
def show_config():
    global OUTPUT_PATTERN, BOOK_OUTPUT_PATTERN, UNPUBLISHED_PLACEHOLDER, API_KEY, MODEL_NAME
    global AUTHOR_FMT_PAPER, AUTHOR_FMT_BOOK, TITLE_STYLE, JOURNAL_ALIASES

    cfg_win = tk.Toplevel()
    cfg_win.title(f'{APP_NAME} — Settings')
    cfg_win.geometry('860x760')
    cfg_win.transient(cfg_win.master)
    cfg_win.grab_set()

    # Window layout: form on top, cheatsheet expands below
    cfg_win.grid_columnconfigure(0, weight=1)
    cfg_win.grid_rowconfigure(2, weight=1)

    # ---------- Settings form ----------
    form = tk.Frame(cfg_win)
    form.grid(row=0, column=0, sticky='we', padx=8, pady=8)
    form.grid_columnconfigure(0, weight=0)
    form.grid_columnconfigure(1, weight=1)

    r = 0
    tk.Label(form, text='Output Pattern (papers):').grid(row=r, column=0, sticky='e', padx=5, pady=5)
    pat_entry = tk.Entry(form, width=60); pat_entry.insert(0, OUTPUT_PATTERN)
    pat_entry.grid(row=r, column=1, sticky='we', padx=5, pady=5); r += 1

    tk.Label(form, text='Book Output Pattern:').grid(row=r, column=0, sticky='e', padx=5, pady=5)
    book_pat_entry = tk.Entry(form, width=60); book_pat_entry.insert(0, BOOK_OUTPUT_PATTERN)
    book_pat_entry.grid(row=r, column=1, sticky='we', padx=5, pady=5); r += 1

    tk.Label(form, text='Unpublished Placeholder:').grid(row=r, column=0, sticky='e', padx=5, pady=5)
    plc_entry = tk.Entry(form, width=60); plc_entry.insert(0, UNPUBLISHED_PLACEHOLDER)
    plc_entry.grid(row=r, column=1, sticky='we', padx=5, pady=5); r += 1

    tk.Label(form, text='Author Format (papers):').grid(row=r, column=0, sticky='e', padx=5, pady=5)
    paper_fmt_entry = tk.Entry(form, width=60); paper_fmt_entry.insert(0, AUTHOR_FMT_PAPER)
    paper_fmt_entry.grid(row=r, column=1, sticky='we', padx=5, pady=5); r += 1

    tk.Label(form, text='Author Format (books):').grid(row=r, column=0, sticky='e', padx=5, pady=5)
    book_fmt_entry = tk.Entry(form, width=60); book_fmt_entry.insert(0, AUTHOR_FMT_BOOK)
    book_fmt_entry.grid(row=r, column=1, sticky='we', padx=5, pady=5); r += 1

    tk.Label(form, text='Gemini API Key:').grid(row=r, column=0, sticky='e', padx=5, pady=5)
    api_entry_cfg = tk.Entry(form, width=60, show='*'); api_entry_cfg.insert(0, API_KEY)
    api_entry_cfg.grid(row=r, column=1, sticky='we', padx=5, pady=5); r += 1

    tk.Label(form, text='Model:').grid(row=r, column=0, sticky='e', padx=5, pady=5)
    model_entry = tk.Entry(form, width=60); model_entry.insert(0, MODEL_NAME)
    model_entry.grid(row=r, column=1, sticky='we', padx=5, pady=5); r += 1

    tk.Label(form, text='Title capitalization:').grid(row=r, column=0, sticky='e', padx=5)
    title_style_var = tk.StringVar(value=TITLE_STYLE)
    ttk.Combobox(form, textvariable=title_style_var, values=('preserve', 'title'), state='readonly').grid(row=r, column=1, sticky='w', padx=5)
    r += 1
    tk.Label(form, text='Journal aliases\n(alias = full name):').grid(row=r, column=0, sticky='ne', padx=5)
    aliases_entry = scrolledtext.ScrolledText(form, height=4, width=55)
    aliases_entry.insert('1.0', JOURNAL_ALIASES)
    aliases_entry.grid(row=r, column=1, sticky='ew', padx=5, pady=5)
    r += 1

    def save_and_close():
        global OUTPUT_PATTERN, BOOK_OUTPUT_PATTERN, UNPUBLISHED_PLACEHOLDER, API_KEY, MODEL_NAME
        global AUTHOR_FMT_PAPER, AUTHOR_FMT_BOOK, TITLE_STYLE, JOURNAL_ALIASES
        aliases = aliases_entry.get('1.0', 'end-1c')
        try:
            parse_aliases(aliases)
        except ValueError as exc:
            messagebox.showerror('Journal aliases', str(exc), parent=cfg_win)
            return
        TITLE_STYLE = title_style_var.get()
        JOURNAL_ALIASES = aliases
        OUTPUT_PATTERN = pat_entry.get().strip() or DEFAULT_OUTPUT_PATTERN
        BOOK_OUTPUT_PATTERN = book_pat_entry.get().strip() or DEFAULT_BOOK_OUTPUT_PATTERN
        UNPUBLISHED_PLACEHOLDER = plc_entry.get().strip() or DEFAULT_UNPUBLISHED
        AUTHOR_FMT_PAPER = paper_fmt_entry.get().strip() or DEFAULT_AUTHOR_FMT_PAPER
        AUTHOR_FMT_BOOK  = book_fmt_entry.get().strip() or DEFAULT_AUTHOR_FMT_BOOK
        API_KEY   = api_entry_cfg.get().strip() or API_KEY
        MODEL_NAME = model_entry.get().strip() or DEFAULT_MODEL
        save_config()
        cfg_win.destroy()

    tk.Button(form, text='Save', command=save_and_close).grid(row=r, column=1, sticky='e', padx=5, pady=10)

    # ---------- Cheatsheet (below Settings) ----------
    tk.Label(cfg_win, text='Cheatsheet', font=('TkDefaultFont', 10, 'bold')).grid(
        row=1, column=0, sticky='w', padx=8, pady=(0, 0)
    )

    cheat_text = scrolledtext.ScrolledText(cfg_win, width=90, height=18)
    cheat_text.grid(row=2, column=0, sticky='nsew', padx=8, pady=(2, 8))

    cheat_text.insert('1.0',
        "Tokens for filename patterns:\n"
        "  {journal}  {year}  {authors}  {title}\n"
        "\n"
        "Author tokens (inside Author Format): {first}, {middle}, {surname}, "
        "{first_initial}, {middle_initials}, {surname_initial}, {suffix}\n"
        "\n"
        f"Defaults:\n  Papers: {DEFAULT_OUTPUT_PATTERN}\n  Books: {DEFAULT_BOOK_OUTPUT_PATTERN}\n"
        "\nCommon author formats:\n  Papers → {DEFAULT_AUTHOR_FMT_PAPER}\n  Books  → {DEFAULT_AUTHOR_FMT_BOOK}\n"
        "\nNotes:\n- {journal} for books = publisher.\n- Authors joined by ', '.\n"
    )
    cheat_text.config(state='disabled')


def show_help_config():
    win = tk.Toplevel()
    win.title(f'{APP_NAME} — Help & User Guide')
    txt = scrolledtext.ScrolledText(win, width=100, height=34)
    txt.pack(fill='both', expand=True, padx=6, pady=6)

    help_text = f"""
{APP_NAME} — Help & User Guide

What this program does (brief):
- It reads ONLY the first N pages ("Pages to extract/read") of each PDF to capture front-matter.
- It sends that small snippet to Gemini (model: gemini-2.5-flash-lite by default).
- The model is asked to return JSON metadata: Authors[], Year, Journal, Title,
  with optional source page numbers and quotations for locating the fields.
  • For books, Journal is interpreted as the Publisher.
- The app automatically renames eligible files using your templates and keeps results needing attention
  unchanged. Review any result against the analyzed pages when you want to check or correct it.

Basic workflow:
1) Select input
   • Click “Browse Files” to choose specific PDFs, or
   • Click “Browse Folder” to process ALL PDFs under a folder (recursive).
   • These are mutually exclusive—use one or the other.

2) Choose mode
   • “Book mode” ON: uses your Book Output Pattern and Book author-format.
   • “Book mode” OFF: uses the paper defaults.
   • Toggling Book mode will adjust the default number of pages read.

3) Set “Pages to extract” (a.k.a. pages to read)
   • Controls how many first pages are analyzed. 
   • Papers default to 4; Books default to 20 when Book mode is toggled ON.
   • If your files have longer prefaces or title pages, increase this value (allowed range: 1–50).

4) Process PDFs / Stop
   • Click “Process PDFs” to extract metadata and automatically rename eligible files.
   • Automatic renaming requires a title, authors, and a four-digit year. A missing journal or publisher
     is allowed, including for working papers. Incomplete results, errors, and conflicts stay unchanged.
   • Process PDFs reuses matching extraction results across batches. It never trusts filenames alone.
     Check Force fresh extraction to bypass the cache; there is no required filename approval step.
   • “Stop” takes effect between files; an in-flight request finishes first.
   • Already completed renames remain undoable. The log shows results, skips, and errors.
   • Read the results by metadata and status; “Needs attention only” focuses on files requiring action.
   • “Export all results...” saves every row in the current batch as a CSV, including rows hidden
     by the attention filter. The report includes original/current paths, metadata, status, page
     counts, and problem details. Export is available when processing is idle and makes no API request.
   • “Retry failed files” retries extraction errors only and keeps completed results and batch totals.
     It uses saved per-file page counts, mode, and naming settings, plus the current API key/model.
     A separate “Retrying: X / failed count” counter tracks this attempt. Successful retries are
     renamed as a new undoable batch; metadata gaps and filename conflicts remain for review.
   • After stopping a retry, remaining errors can be retried again. Any successfully extracted file
     held before renaming can be applied with “Continue batch” or “Review document...”.
   • Select a result and use “Reanalyze file...” to read a different number of first pages for only
     that file. It sends a new Gemini request, including for files already renamed.
     Success replaces the metadata and review pages; failed requests preserve the previous result.
     Review the new result and explicitly apply a correction to change its filename.
     Its page count is saved for later continuation; batch totals and undo history are retained.
   • The latest batch is saved after each file, including metadata and the exact analyzed PDF pages.
     On restart, saved results are restored and files checked without model requests or renames.
     “Continue batch” reuses unchanged files' metadata and processes pending, failed, or changed files.
     Missing files must be restored to the displayed path; their locations are not guessed.
   • Extraction cache matches content hash, pages, paper/book mode, model and extraction rules.
     Cached batches need no API key. Reanalyze file always requests fresh metadata.
     Config → Clear extraction cache removes cached results and attempt records; batch and undo stay intact.
   • Starting a new Process PDFs batch replaces the saved batch and its generated review cache.

5) Review a document when needed
   • Double-click any result or use “Review document...”, including for already renamed files.
   • The viewer shows the exact saved prefix sent to Gemini: up to N pages, or the whole PDF if shorter.
   • Use Previous / Next, enter a page and click Go, or change zoom to inspect every analyzed page.
   • Optional source quotations and PDF page buttons help locate authors, year, journal/publisher, title.
     These are model-provided navigation aids, not independently verified evidence.
   • Page numbers count physical PDF pages starting at 1, including covers and front matter;
     they do not refer to printed page labels. Missing source locators do not block review.
   • Correct authors (one per line), year, journal/publisher, and title alongside the document.

6) Apply correction
   • “Apply correction” uses the edited metadata locally without another model request.
   • An optional filename override must include .pdf. Review any suggested distinct filename first.
   • Each correction that renames a file creates a separate undo batch, including corrections to files
     already renamed by this session. Source contents are checked again before applying.
   • An occupied destination is refused, never overwritten or silently changed. Open “Review document...”
     again to inspect the newly suggested distinct filename or enter your own override.

7) Undo last batch
   • Restores the latest batch with outstanding changes, including after restarting the app.
   • Undo refuses changed file contents or an occupied original filename. Resolve the issue and retry.
   • Review follows restored paths, and the latest batch preserves them across restarts.

Settings overview (Config → Settings…):
- Output Pattern (papers)
  Template for non-book files. Default: {DEFAULT_OUTPUT_PATTERN}

- Book Output Pattern
  Template for books. Default: {DEFAULT_BOOK_OUTPUT_PATTERN}
  Note: here {{journal}} stands for the publisher.

- Unpublished Placeholder
  Used when {{journal}} (or publisher for books) is missing. Default: "{DEFAULT_UNPUBLISHED}".

- Author Format (papers), Author Format (books)
  Templated rule used to render EACH author before joining with ", ".
  Common choices:
    • Papers: {DEFAULT_AUTHOR_FMT_PAPER}
    • Books:  {DEFAULT_AUTHOR_FMT_BOOK}
  Author tokens available:
    {{first}}, {{middle}}, {{surname}} (aliases: {{last}}, {{family}}),
    {{first_initial}}, {{middle_initials}}, {{surname_initial}}, {{suffix}}
  Punctuation is literal—include commas/dots where you want them.

- Gemini API Key
  Required to call the model. Your key is stored locally in the app’s config folder.

- Model
  Defaults to "{DEFAULT_MODEL}". You can override if needed.

- Embedded Cheatsheet (inside Settings)
  A compact reference of patterns, tokens, and examples.

How renaming is decided:
A) Metadata extraction:
   The app reads the first N pages and asks the model to return JSON with:
     authors (array), year (string), journal (string), title (string).
   • An existing filename that looks formatted does not skip extraction.
   • New model responses must match the schema exactly. Invalid types, keys, years or citations
     are extraction errors available for retry. Empty fields remain unchanged for review.
   • Source capitalization is preserved by default. Settings can apply title case and journal aliases.
     Journal aliases do not change book publishers.
   • Structured author names keep full compound surnames; organizations use their full names.
   • Version dates include page/quote locators. Published articles prefer publication then online year;
     preprints prefer the latest reported revision; books use the current edition's publication year.
     Ambiguous dates or author types stay unchanged for review.
   • Complete metadata is not a guarantee of factual accuracy; successful results can also be reviewed.

B) Filename building:
   • {{authors}} uses structured given/family/suffix parts and your author-format template,
     then joins authors with ", ". Organization and unknown authors keep their full literal names.
     Review document → Name parts... corrects name structure locally. A year choice jumps to its page.
   • The final filename is created via your Output Pattern / Book Output Pattern.
   • Illegal filesystem characters are stripped; whitespace is normalized.

C) Collisions & duplicates:
   • If the new name equals the current name → no rename is needed (“Unchanged”).
   • If a file with the target name exists:
       - If contents match (SHA-256) → “Duplicate”; no file is deleted or renamed.
       - Else the file stays unchanged for review. A short hash suffix (e.g., “_1a2b3c4d”) can be
         suggested in the review window for you to inspect and accept.
   • When batch files share a proposed name, the first eligible file may be renamed; later conflicts
     are held for review.
   • Applying a rename checks the source and destination again, without silently choosing another name.

D) Incomplete metadata:
   • Missing title, authors, or four-digit year prevents automatic renaming in either mode.
   • Missing journal/publisher alone is allowed and uses the Unpublished Placeholder.
   • Extraction failures can be retried with “Retry failed files”, or corrected locally when possible.
   • A filename override can be used when you want to enter the complete name manually.

E) Persistent undo:
   • A local journal records applied renames, file paths, and content hashes.
   • Undo checks the renamed contents and original path before restoring a name.
   • Failed undo entries remain available for retry. Journal errors require review before continuing.

First run:
- If no config file exists, a Setup Guide pops up automatically.
  Use it to open Settings, paste API key, and review templates.

Privacy & scope:
- Only the first N pages are uploaded during processing, retry, reanalysis, or continuation needing extraction.
  Reusing cached metadata makes no model request. Uploaded Gemini snippets are
  deleted on a best-effort basis when the extraction attempt finishes, including after errors.
- The latest batch's metadata, file paths, hashes, progress, and analyzed prefixes are saved locally
  under batch-state. Temporary working copies are cleaned up on exit; saved review copies persist.
  Starting a new batch replaces the saved results and removes the previous generated review cache.
- Cross-batch extraction results, PDF prefixes and attempt records persist in extraction-cache.sqlite3.
  Clear them in Config → Clear extraction cache. API keys and raw provider errors are not recorded there.
  To remove saved results manually, close the app and remove batch-state from its storage directory.
  API credentials are not part of the batch settings. The undo journal remains independent.
- A crash between a provider response and its checkpoint can cause that request to be repeated.
- Browsing pages, editing, applying corrections, and undoing are local; they make no model requests.
- Extraction quality and cost depend on the documents, page count, and selected model.
- Model name parts and date quotations can be wrong. Review allows local correction without a new request.
- Old unstructured names use heuristic parsing. Keep preserve capitalization for unfamiliar acronyms.

Troubleshooting:
- “Gemini API key is required” → Set your key in Settings.
- Invalid JSON/schema → Check the selected model and use Retry failed files. Invalid responses are not cached.
- Repeated “Unchanged” status → Your template currently evaluates to the existing filename.
- Wrong author format → Adjust the Author Format fields in Settings; use tokens correctly.
- Unexpected publisher/journal → For books, {{journal}} is the publisher by design.
- Source changed → Continue batch to extract fresh metadata before renaming.
- Destination occupied → Open Review document... and inspect the suggested distinct name or override it.
- Missing/wrong source locator → Browse the analyzed pages; use Reanalyze file... to read more if needed.
- Missing file → Restore it to the displayed path, then Continue batch.
- Saved results cannot be loaded → Check the reported checkpoint path. The unreadable file is kept.
- Recovery needed → Resolve the interrupted move's paths or use Undo last batch.
- Undo failed → Check for changed, moved, or missing files and an occupied original filename.

Config & storage:
- Config file: created under your user directory (pdf_metadata_renamer.config).
- ENV file (optional API key): pdf_metadata_renamer.env in the same folder.
- Both are created in the app’s storage directory shown by your OS (LOCALAPPDATA on Windows; home on others).
- Undo journal: the “rename-history” subfolder of the app storage directory shown in About.
- Saved latest batch and review pages: the “batch-state” subfolder of the same directory.

Tip:
- If extraction misses data on certain PDFs, raise “Pages to extract”.
- Keep Book mode consistent within a batch for predictable naming.

"""
    txt.insert('1.0', help_text)
    txt.config(state='disabled')

# =========================
# First-run setup guide
# =========================
def show_setup_guide(root):
    win = tk.Toplevel(root)
    win.title(f'Welcome — {APP_NAME} Setup Guide')
    win.attributes('-topmost', True)
    msg = (
        f"Welcome to {APP_NAME}.\n\n"
        "Before first use:\n"
        "  1) Open Config → Settings…\n"
        "  2) Paste your Gemini API key\n"
        "  3) Review filename templates\n"
        "     • Papers: {journal} - {year} - {authors} - {title}.pdf\n"
        "     • Books:  {authors} - {title} - {journal} ({year}).pdf\n"
        "  4) Set author formats (papers vs. books)\n\n"
        "Then select PDFs, set the first N pages to read, and click Process PDFs.\n"
        "Files with complete required metadata are renamed automatically.\n"
        "Use Needs attention only to find incomplete results or conflicts.\n"
        "Double-click any result to review all analyzed pages beside its metadata.\n"
        "Use Apply correction to apply local edits without another model request.\n"
        "Undo last batch can restore names even after restarting the app.\n"
        "The latest results and review pages are saved; use Continue batch after restarting.\n"
        "Stop takes effect between files; an in-flight request finishes first.\n\n"
        "Tip: The Settings window includes a Cheatsheet section."
    )
    tk.Label(win, text=msg, justify='left').pack(padx=10, pady=10)
    btns = tk.Frame(win); btns.pack(pady=(0,10))
    tk.Button(btns, text='Open Settings', command=show_config).pack(side='left', padx=6)
    tk.Button(btns, text='Close', command=win.destroy).pack(side='left', padx=6)

# =========================
# PDF extraction
# =========================
# Shared headless services; GUI wrappers supply the current settings and callbacks.
import paperdf_core as core
import paperdf_naming as naming_service
from paperdf_core import extract_first_n_pages
from paperdf_gemini import (_metadata_text, _author_name, _metadata_authors, _snippet_page_count,
                            _metadata_output_schema, _normalize_evidence, _metadata_all_empty,
                            _LazyGeminiClient, get_metadata_from_snippet as gemini_metadata)
from paperdf_naming import _parse_author, _initial, _middle_initials, _render_author


def get_metadata_from_snippet(pdf_bytes, is_book, sdk_client=None, model=None):
    return gemini_metadata(pdf_bytes, is_book, sdk_client or client, model or MODEL_NAME)


def format_authors_list(authors_list, is_book, author_format=None, author_details=None):
    fmt = author_format if author_format is not None else (AUTHOR_FMT_BOOK if is_book else AUTHOR_FMT_PAPER)
    return naming_service.format_authors_list(authors_list, is_book, fmt, author_details)


def build_new_filename(meta, is_book=False, naming_settings=None):
    naming = {'pattern': BOOK_OUTPUT_PATTERN if is_book else OUTPUT_PATTERN,
              'author_format': AUTHOR_FMT_BOOK if is_book else AUTHOR_FMT_PAPER,
              'unpublished': UNPUBLISHED_PLACEHOLDER,
              'title_style': TITLE_STYLE if naming_settings is None else 'preserve',
              'journal_aliases': JOURNAL_ALIASES if naming_settings is None else ''}
    naming.update(naming_settings or {})
    return naming_service.build_new_filename(meta, is_book, naming)


def prepare_preview(file_list, pages, is_book, sdk_client, model, cancelled, on_progress,
                    review_dir=None, cache=None, force=False):
    return core.prepare_preview(file_list, pages, is_book, sdk_client, model or MODEL_NAME,
                                cancelled, on_progress, review_dir, cache, force,
                                extractor=get_metadata_from_snippet)


def extract_rows(rows, pages, is_book, sdk_client, model, cancelled, on_progress, review_dir=None, cache=None):
    return core.extract_rows(rows, pages, is_book, sdk_client, model, cancelled, on_progress,
                             review_dir, cache, preview=prepare_preview)


def retry_failed_extractions(rows, pages, is_book, sdk_client, model, cancelled, on_progress, review_dir=None):
    return extract_rows([row for row in rows if can_retry_extraction(row)], pages, is_book,
                        sdk_client, model, cancelled, on_progress, review_dir)


def reanalyze_row(row, pages, is_book, sdk_client, model, cancelled, review_dir=None, cache=None):
    return core.reanalyze_row(row, pages, is_book, sdk_client, model, cancelled,
                              review_dir, cache, preview=prepare_preview)


def main():
    global selected_files, files_entry, folder_entry
    selected_files = []
    review_session = tempfile.TemporaryDirectory(prefix='paperdf-review-')
    batch_store = BatchStore(os.path.join(STORE_DIR, 'batch-state'))
    extraction_cache = ExtractionCache(os.path.join(STORE_DIR, 'extraction-cache.sqlite3'))
    root = tk.Tk()
    root.title(f"{APP_NAME} {APP_VERSION}")
    root.geometry('1120x820')
    root.minsize(850, 650)
    try:
        root._icon_image = PhotoImage(file=resource_path('assets/icon.png'))
        root.wm_iconphoto(True, root._icon_image)
    except Exception:
        pass

    menubar = tk.Menu(root)
    cfg = tk.Menu(menubar, tearoff=0)
    cfg.add_command(label='Settings...', command=show_config)
    cfg.add_command(label='Help...', command=show_help_config)
    cfg.add_command(label='About...', command=lambda: show_about_dialog(root))
    def clear_extraction_cache():
        if busy:
            return
        if messagebox.askyesno('Clear extraction cache',
                               'Delete cached extraction results and their attempt records?\n'
                               'Future batches may need new Gemini requests. Saved batch results and undo history are kept.',
                               parent=root):
            try:
                extraction_cache.clear()
            except Exception as exc:
                messagebox.showerror('Cache not cleared', str(exc), parent=root)
            else:
                log('Extraction cache and attempt records cleared.\n')
    cfg.add_command(label='Clear extraction cache...', command=clear_extraction_cache)
    menubar.add_cascade(label='Config', menu=cfg)
    root.config(menu=menubar)
    root.columnconfigure(0, weight=1)
    root.rowconfigure(1, weight=3)
    root.rowconfigure(2, weight=1)
    inputs = ttk.Frame(root, padding=10)
    inputs.grid(row=0, column=0, sticky='ew')
    inputs.columnconfigure(1, weight=1)
    ttk.Label(inputs, text='PDF Folder:').grid(row=0, column=0, sticky='e', padx=5)
    folder_entry = ttk.Entry(inputs)
    folder_entry.grid(row=0, column=1, sticky='ew', padx=5, pady=4)
    folder_btn = ttk.Button(inputs, text='Browse Folder', command=lambda: browse_folder(folder_entry))
    folder_btn.grid(row=0, column=2, padx=5)
    ttk.Label(inputs, text='Or select PDF Files:').grid(row=1, column=0, sticky='e', padx=5)
    files_entry = ttk.Entry(inputs, state='readonly')
    files_entry.grid(row=1, column=1, sticky='ew', padx=5, pady=4)
    files_btn = ttk.Button(inputs, text='Browse Files', command=select_files)
    files_btn.grid(row=1, column=2, padx=5)
    options = ttk.Frame(inputs)
    options.grid(row=2, column=0, columnspan=3, sticky='ew', pady=5)
    ttk.Label(options, text='Pages to extract:').pack(side='left', padx=5)
    pages_entry = ttk.Entry(options, width=5)
    pages_entry.insert(0, str(DEFAULT_PAPER_PAGES))
    pages_entry.pack(side='left', padx=5)
    book_var = tk.BooleanVar(value=False)

    def toggle_book():
        pages_entry.delete(0, tk.END)
        pages_entry.insert(0, str(DEFAULT_BOOK_PAGES if book_var.get() else DEFAULT_PAPER_PAGES))

    book_btn = ttk.Checkbutton(options, text='Book mode', variable=book_var, command=toggle_book)
    book_btn.pack(side='left', padx=10)
    force_var = tk.BooleanVar(value=False)
    force_btn = ttk.Checkbutton(inputs, text='Force fresh extraction (ignore cache)', variable=force_var)
    force_btn.grid(row=3, column=0, columnspan=3, sticky='w', padx=5)
    busy = False
    closing = False
    status = tk.StringVar(value='Process PDFs in one click. Review individual documents when needed.')
    logw = scrolledtext.ScrolledText(root, height=7)
    logw.grid(row=2, column=0, sticky='nsew', padx=10, pady=5)
    footer = ttk.Frame(root, padding=10)
    footer.grid(row=3, column=0, sticky='ew')
    footer.columnconfigure(0, weight=1)
    ttk.Label(footer, textvariable=status).grid(row=0, column=0, sticky='w')
    batch_progress = {'total': 0, 'analyzed': 0, 'renamed': 0, 'phase': 'rename',
                      'retry_done': 0, 'retry_total': 0}
    retry_context = {}
    progress_text = tk.StringVar(value='Renamed: 0 / 0 files  ·  Analyzed: 0 / 0')
    ttk.Label(footer, textvariable=progress_text).grid(row=1, column=0, sticky='w', pady=(5, 0))
    progress = ttk.Progressbar(footer, mode='determinate')
    progress.grid(row=2, column=0, sticky='ew', pady=5)

    def refresh_progress():
        total = batch_progress['total']
        if total is None:
            progress_text.set('Counting PDF files…  ·  Renamed: 0')
            progress.configure(value=0, maximum=1)
            return
        analyzed, renamed = batch_progress['analyzed'], batch_progress['renamed']
        if batch_progress['phase'] in ('retry', 'continue'):
            done, failed = batch_progress['retry_done'], batch_progress['retry_total']
            action = 'Retrying' if batch_progress['phase'] == 'retry' else 'Continuing'
            progress_text.set(f'{action}: {done} / {failed}  ·  Renamed: {renamed} / {total} files  ·  Analyzed: {analyzed} / {total}')
            progress.configure(value=done, maximum=max(1, failed))
            return
        elif batch_progress['phase'] == 'analysis':
            progress_text.set(f'Analyzed: {analyzed} / {total} files  ·  Renamed: {renamed} / {total}')
            value = analyzed
        else:
            progress_text.set(f'Renamed: {renamed} / {total} files  ·  Analyzed: {analyzed} / {total}')
            value = renamed
        progress.configure(value=value, maximum=max(1, total))

    def update_counts(renamed, total):
        batch_progress.update(renamed=renamed, total=total)
        refresh_progress()

    def log(message):
        logw.insert(tk.END, message)
        logw.see(tk.END)

    def set_busy(value):
        nonlocal busy
        busy = value
        for widget in (folder_entry, folder_btn, files_btn, pages_entry, book_btn, force_btn, process_btn):
            widget.configure(state='disabled' if value else 'normal')
        cfg.entryconfigure(0, state='disabled' if value else 'normal')
        cfg.entryconfigure('Clear extraction cache...', state='disabled' if value else 'normal')
        stop_btn.configure(state='normal' if value else 'disabled')
        panel.set_enabled(not value)
        status.set('Working… Stop takes effect between files.' if value else 'Batch results are ready. Review any document or undo the last batch.')
        if closing and not value:
            root.after_idle(root.destroy)

    def update_selected_paths(results):
        replacements = {os.path.normcase(r.source): r.destination for r in results}
        selected_files[:] = [replacements.get(os.path.normcase(os.path.abspath(p)), p) for p in selected_files]

    panel = ResultsPanel(root, os.path.join(STORE_DIR, 'rename-history'),
                         lambda callback: run_on_ui(root, callback), set_busy, log, stop_event,
                         update_selected_paths, update_counts, checkpoint=batch_store.save)
    panel.grid(row=1, column=0, sticky='nsew', padx=10, pady=5)

    def continue_saved(retry_only=False):
        failed = [row for row in panel.rows if (can_retry_extraction(row) if retry_only else can_continue(row))]
        if busy or not failed or not retry_context:
            return
        # Reuse the batch's document settings; allow fixing API credentials/model.
        pages, is_book = retry_context['pages'], retry_context['is_book']
        api_key, model = API_KEY, MODEL_NAME
        batch_progress.update(phase='retry' if retry_only else 'continue', retry_done=0, retry_total=len(failed))
        refresh_progress()
        log(f"{'Retrying failed files' if retry_only else 'Continuing saved batch'}: {len(failed)} file(s).\n")

        def on_progress(index, total, row):
            batch_store.save()
            analyzed = sum(item.get('analysis_attempted', True) for item in panel.rows)
            status_message = f"{row['status']}: {row['source']} {row.get('message', '')}\n"

            def update():
                batch_progress.update(retry_done=index, retry_total=total, analyzed=analyzed)
                panel._render()
                log(status_message)

            run_on_ui(root, update)

        def operation():
            validate_rows(panel.rows, stop_event.is_set)
            batch_store.save()
            candidates = failed if retry_only else [row for row in panel.rows if can_continue(row)]
            to_extract = [row for row in candidates if row.get('resume_action') == 'extract']
            ready = [row for row in candidates if row.get('resume_action') == 'rename' and
                     not row.get('pending_move')]
            sdk_client = None
            retried = []
            try:
                if to_extract and not stop_event.is_set():
                    sdk_client = _LazyGeminiClient(api_key)
                    retried = extract_rows(to_extract, pages, is_book, sdk_client, model,
                                           stop_event.is_set, on_progress, review_session.name, cache=extraction_cache)
                    if sdk_client.error is not None:
                        raise sdk_client.error
            finally:
                if sdk_client is not None:
                    try:
                        sdk_client.close()
                    except Exception:
                        logging.warning('Could not close Gemini client', exc_info=True)
            if stop_event.is_set():
                for row in retried + ready:
                    if not row.get('needs_review'):
                        row.update(status='Stopped', needs_review=True,
                                   message='Stopped before renaming. Continue batch or review this document to apply locally.')
                return None
            for row in ready:
                row.update(needs_review=False, selected=True)
            return process_rows(retried + ready, panel.build_name, panel.journal_dir,
                                stop_event.is_set, panel._report, panel._before_rename)

        def done(result, error):
            batch_progress['phase'] = 'rename'
            panel._completed(result, error)

        panel._work(operation, done)

    panel.on_retry = lambda: continue_saved(retry_only=True)
    panel.on_continue = continue_saved

    def reanalyze_selected(row):
        if busy or not retry_context:
            return
        if row.get('pending_move') or row.get('status') == 'Recovery needed':
            messagebox.showerror('Recovery needed', 'Resolve the interrupted move before reanalyzing.', parent=root)
            return
        previous_pages = row.get('requested_pages', retry_context['pages'])
        pages = simpledialog.askinteger(
            'Reanalyze this file',
            f"{os.path.basename(row['source'])}\nPreviously requested: first {previous_pages} pages.\n\n"
            'Read how many pages from the beginning?\n'
            'This sends a new Gemini request. On success, new metadata replaces the current metadata; '
            'review it before applying a filename correction. Failed requests keep the current result.',
            initialvalue=previous_pages, minvalue=1, maxvalue=MAX_PAGES_TO_EXTRACT, parent=root)
        if pages is None:
            return
        api_key, model = API_KEY, MODEL_NAME
        is_book = retry_context['is_book']

        def operation():
            if not api_key:
                raise ValueError('Set your Gemini API key in Config → Settings to reanalyze this file.')
            sdk_client = genai.Client(api_key=api_key)
            try:
                return reanalyze_row(row, pages, is_book, sdk_client, model,
                                     stop_event.is_set, review_session.name, cache=extraction_cache)
            finally:
                try:
                    sdk_client.close()
                except Exception:
                    logging.warning('Could not close Gemini client', exc_info=True)

        def done(result, error):
            batch_progress.update(phase='rename', analyzed=sum(
                item.get('analysis_attempted', True) for item in panel.rows))
            panel._completed(result, error)
            if result:
                log(f'Reanalyzed first {pages} pages: {row["source"]}. Review document to apply corrections.\n')

        panel._work(operation, done)

    panel.on_reanalyze = reanalyze_selected

    def process():
        folder = folder_entry.get().strip()
        if not selected_files and not folder:
            messagebox.showerror('Select input', 'Select PDF files or a folder.', parent=root)
            return
        try:
            pages = int(pages_entry.get().strip())
            if not 1 <= pages <= MAX_PAGES_TO_EXTRACT:
                raise ValueError()
        except ValueError:
            messagebox.showerror('Page count', f'Enter a whole number between 1 and {MAX_PAGES_TO_EXTRACT}.', parent=root)
            return
        if selected_files and folder:
            messagebox.showerror('Select input', 'Choose files or a folder, not both.', parent=root)
            return
        items = list(selected_files)
        # Capture Tk values and configuration on the UI thread before starting work.
        is_book, api_key, model = book_var.get(), API_KEY, MODEL_NAME
        naming = {'pattern': BOOK_OUTPUT_PATTERN if is_book else OUTPUT_PATTERN,
                  'author_format': AUTHOR_FMT_BOOK if is_book else AUTHOR_FMT_PAPER,
                  'unpublished': UNPUBLISHED_PLACEHOLDER, 'title_style': TITLE_STYLE, 'journal_aliases': JOURNAL_ALIASES}
        retry_context.update(pages=pages, is_book=is_book, naming=naming, force_refresh=force_var.get())
        stop_event.clear()
        set_busy(True)
        logw.delete('1.0', tk.END)
        total = len({os.path.normcase(os.path.abspath(path)) for path in items}) if items else None
        batch_progress.update(total=total, analyzed=0, renamed=0, phase='analysis')
        panel.rows = []
        panel.total_files = total
        panel._render()

        def on_progress(index, total, row):
            batch_store.save()
            def update():
                batch_progress.update(analyzed=sum(item.get('analysis_attempted', True) for item in batch_store.data['rows']), total=total)
                refresh_progress()
                log(f"{row['status']} [{row.get('extraction_source', 'pending')}]: {row['source']} {row.get('message') or row.get('reason') or row.get('extraction_error', '')}\n")
            run_on_ui(root, update)

        def finish(rows, error):
            set_busy(False)
            if closing:
                return
            batch_progress['phase'] = 'rename'
            if error:
                messagebox.showerror('Processing failed', error, parent=root)
            outcome = 'failed' if error else ('stopped' if stop_event.is_set() else 'complete')
            log(f"Extraction {outcome}: {len(rows)} file(s).\n")
            panel.load(rows, lambda meta: build_new_filename(meta, is_book, naming), is_book,
                       automatic=not error, total_files=batch_progress['total'])
            if not panel.busy:
                try:
                    batch_store.save()
                except Exception as exc:
                    messagebox.showerror('Progress not saved', str(exc), parent=root)

        def worker():
            sdk_client = None
            rows, error = [], None
            try:
                if not items:
                    for directory, _, names in os.walk(folder):
                        if stop_event.is_set():
                            break
                        items.extend(os.path.join(directory, name) for name in names if name.lower().endswith('.pdf'))
                total = len({os.path.normcase(os.path.abspath(path)) for path in items})

                def show_total():
                    panel.total_files = total
                    panel._update_counts()

                run_on_ui(root, show_total)
                if not items and not stop_event.is_set():
                    raise ValueError('No PDF files found. Check the selected folder.')
                # Persist every input before the first model call, including unstarted files.
                rows = batch_store.start(items, retry_context)
                if stop_event.is_set():
                    return
                sdk_client = _LazyGeminiClient(api_key)
                extract_rows(rows, pages, is_book, sdk_client, model, stop_event.is_set, on_progress,
                             review_dir=review_session.name, cache=extraction_cache)
                if sdk_client.error is not None:
                    raise sdk_client.error
            except Exception as exc:
                error = str(exc)
            finally:
                if sdk_client is not None:
                    try:
                        sdk_client.close()
                    except Exception:
                        logging.warning('Could not close Gemini client', exc_info=True)
                run_on_ui(root, lambda: finish(rows, error))

        threading.Thread(target=worker, daemon=True).start()

    process_btn = ttk.Button(options, text='Process PDFs', command=process)
    process_btn.pack(side='left', padx=10)
    stop_btn = ttk.Button(options, text='Stop', state='disabled', command=stop_event.set)
    stop_btn.pack(side='left', padx=5)

    def close():
        nonlocal closing
        if busy:
            closing = True
            stop_event.set()
            status.set('Finishing the current file before closing…')
        else:
            root.destroy()

    ttk.Button(footer, text='Exit', command=close).grid(row=0, column=1, rowspan=3, padx=10)
    root.protocol('WM_DELETE_WINDOW', close)
    def restore_saved():
        if not batch_store.path.exists():
            return

        def operation():
            saved = batch_store.load()
            if saved:
                validate_rows(saved['rows'], stop_event.is_set)
            return saved

        def done(saved, error):
            if saved is None:
                return
            context = saved['context']
            retry_context.update(context)
            rows = saved['rows']
            batch_progress.update(total=len(rows), analyzed=sum(row.get('analysis_attempted', False) for row in rows),
                                  phase='rename')
            book_var.set(context['is_book'])
            pages_entry.delete(0, tk.END)
            pages_entry.insert(0, str(context['pages']))
            selected_files[:] = [row['source'] for row in rows]
            files_entry.configure(state='normal')
            files_entry.delete(0, tk.END)
            files_entry.insert(0, f'{len(rows)} saved file(s)')
            files_entry.configure(state='readonly')
            panel.load(rows, lambda meta: build_new_filename(meta, context['is_book'], context['naming']),
                       context['is_book'], automatic=False, total_files=len(rows), mark_stopped=False)
            status.set('Saved batch restored. Continue batch to finish remaining files.')
            log('Saved batch restored and files checked. No model requests were made.\n')

        panel._work(operation, done)

    # Disable actions before the event loop starts so queued input cannot race restoration.
    restore_saved()
    if FIRST_RUN:
        root.after(200, lambda: show_setup_guide(root))
    try:
        root.mainloop()
    finally:
        try:
            review_session.cleanup()
        except OSError:
            logging.warning('Could not remove temporary review pages', exc_info=True)

# =========================
# Entrypoint
# =========================
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    main()
