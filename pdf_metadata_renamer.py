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
import io
import sys
import logging
import json
import threading
import tempfile
import configparser
import re
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk
from tkinter import PhotoImage

from dotenv import load_dotenv
from PyPDF2 import PdfReader, PdfWriter

# Google AI Studio SDK (pip install google-genai)
from google import genai
from google.genai import types
from paperdf_preview import ResultsPanel
from paperdf_processing import can_retry_extraction, process_rows
from paperdf_session import BatchStore, can_continue, validate_rows
from paperdf_workflow import fingerprint_file
from paperdf_cache import ExtractionCache
from paperdf_schema import parse_metadata

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
# Optional: robust title-casing with fallback
try:
    from titlecase import titlecase  # pip install titlecase
except Exception:
    def titlecase(s: str) -> str:
        return s.title() if isinstance(s, str) else s

# =========================
# Storage locations
# =========================
ENV_FILENAME = 'pdf_metadata_renamer.env'
CONFIG_FILENAME = 'pdf_metadata_renamer.config'
ENV_SUBDIR = 'pdfrenamer'

HOME_DIR = os.path.expanduser('~')
LOCALAPPDATA_DIR = os.getenv('LOCALAPPDATA', HOME_DIR)
STORE_DIR = os.path.join(LOCALAPPDATA_DIR, ENV_SUBDIR)
os.makedirs(STORE_DIR, exist_ok=True)
ENV_PATH = os.path.join(STORE_DIR, ENV_FILENAME)
CONFIG_PATH = os.path.join(STORE_DIR, CONFIG_FILENAME)

# Load environment variables if present
if os.path.exists(ENV_PATH):
    load_dotenv(dotenv_path=ENV_PATH, override=False)

# =========================
# Defaults & constants
# =========================
API_KEY = os.getenv('GEMINI_API_KEY', '')
DEFAULT_MODEL = 'gemini-2.5-flash-lite'   # default model
MODEL_NAME = DEFAULT_MODEL

INVALID_FILENAME_CHARS = '<>:"/\\|?*'
DEFAULT_OUTPUT_PATTERN = '{journal} - {year} - {authors} - {title}.pdf'
# Default book template as requested
DEFAULT_BOOK_OUTPUT_PATTERN = '{authors} - {title} - {journal} ({year}).pdf'
DEFAULT_UNPUBLISHED = 'Unpublished'
DEFAULT_PAPER_PAGES = 4
DEFAULT_BOOK_PAGES = 20
MAX_PAGES_TO_EXTRACT = 50

# Author format defaults
DEFAULT_AUTHOR_FMT_PAPER = '{surname}'
DEFAULT_AUTHOR_FMT_BOOK = '{surname}, {first_initial}.'

# Detect first run (no config file yet)
FIRST_RUN = not os.path.exists(CONFIG_PATH)

# =========================
# Load or initialize config
# =========================
config = configparser.ConfigParser()
if os.path.exists(CONFIG_PATH):
    config.read(CONFIG_PATH)
if 'Settings' not in config:
    config['Settings'] = {}
settings = config['Settings']

OUTPUT_PATTERN = settings.get('output_pattern', DEFAULT_OUTPUT_PATTERN)
BOOK_OUTPUT_PATTERN = settings.get('book_output_pattern', DEFAULT_BOOK_OUTPUT_PATTERN)
UNPUBLISHED_PLACEHOLDER = settings.get('unpublished', DEFAULT_UNPUBLISHED)
API_KEY = settings.get('api_key', API_KEY)
MODEL_NAME = settings.get('model', MODEL_NAME)

# Separate author formats
AUTHOR_FMT_PAPER = settings.get('author_format_paper', DEFAULT_AUTHOR_FMT_PAPER)
AUTHOR_FMT_BOOK = settings.get('author_format_book', DEFAULT_AUTHOR_FMT_BOOK)

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
    with open(CONFIG_PATH, 'w') as f:
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
    global AUTHOR_FMT_PAPER, AUTHOR_FMT_BOOK

    cfg_win = tk.Toplevel()
    cfg_win.title(f'{APP_NAME} — Settings')
    cfg_win.geometry('860x560')
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

    def save_and_close():
        global OUTPUT_PATTERN, BOOK_OUTPUT_PATTERN, UNPUBLISHED_PLACEHOLDER, API_KEY, MODEL_NAME
        global AUTHOR_FMT_PAPER, AUTHOR_FMT_BOOK
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
   • “Journal” is title-cased; for books it is treated as the publisher.
   • Complete metadata is not a guarantee of factual accuracy; successful results can also be reviewed.

B) Filename building:
   • {{authors}} is built by parsing each name into parts (first/middle/surname/suffix)
     and rendering them with your author-format template, then joining with ", ".
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
- Name parsing is heuristic and may need manual edits for unusual name orders or capitalization.

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
def extract_first_n_pages(pdf_path: str, n: int) -> bytes:
    if n < 1:
        raise ValueError('Pages to extract must be at least 1.')
    reader = PdfReader(pdf_path)
    writer = PdfWriter()
    page_count = min(n, len(reader.pages))
    for page in reader.pages[:page_count]:
        writer.add_page(page)
    buf = io.BytesIO()
    writer.write(buf)
    buf.seek(0)
    return buf.read()

def _metadata_text(value) -> str:
    if value is None:
        return ''
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value).strip()
    if isinstance(value, list):
        for item in value:
            text = _metadata_text(item)
            if text:
                return text
        return ''
    if isinstance(value, dict):
        for key in ('value', 'name', 'text', 'title', 'journal', 'publisher', 'year'):
            text = _metadata_text(value.get(key))
            if text:
                return text
        return ''
    return str(value).strip()

def _author_name(value) -> str:
    if isinstance(value, dict):
        for key in ('name', 'full_name', 'full', 'author'):
            text = _metadata_text(value.get(key))
            if text:
                return text
        parts = [
            _metadata_text(value.get('first')),
            _metadata_text(value.get('middle')),
            _metadata_text(value.get('surname') or value.get('last') or value.get('family')),
        ]
        suffix = _metadata_text(value.get('suffix'))
        if suffix:
            parts.append(suffix)
        return ' '.join(part for part in parts if part).strip()
    return _metadata_text(value)

def _metadata_authors(value) -> list:
    if not value:
        return []
    if isinstance(value, list):
        return [name for name in (_author_name(item) for item in value) if name]
    if isinstance(value, dict):
        name = _author_name(value)
        return [name] if name else []
    text = _metadata_text(value)
    if not text:
        return []
    separator = ';' if ';' in text else ','
    return [author.strip() for author in text.split(separator) if author.strip()]

# =========================
# Gemini metadata extraction
# =========================
_EVIDENCE_FIELDS = ('authors', 'year', 'journal', 'title')


def _snippet_page_count(pdf_bytes: bytes):
    """Count physical pages without preventing upload cleanup on malformed input."""
    try:
        return len(PdfReader(io.BytesIO(pdf_bytes)).pages)
    except Exception:
        return None


def _metadata_output_schema(page_count=None):
    page_schema = {'type': 'integer', 'minimum': 1}
    if page_count:
        page_schema['maximum'] = page_count
    citation_schema = {
        'type': 'object',
        'properties': {
            'page': page_schema,
            'quote': {'type': 'string', 'description': 'Exact visible text, 1-500 characters'},
        },
        'required': ['page', 'quote'],
        'additionalProperties': False,
    }
    return {
        'type': 'object',
        'properties': {
            'authors': {'type': 'array', 'items': {'type': 'string'}},
            'year': {'type': 'string'},
            'journal': {'type': 'string'},
            'title': {'type': 'string'},
            'evidence': {
                'type': 'object',
                'properties': {
                    field: {'type': 'array', 'items': citation_schema}
                    for field in _EVIDENCE_FIELDS
                },
                'required': list(_EVIDENCE_FIELDS),
                'additionalProperties': False,
            },
        },
        'required': [*_EVIDENCE_FIELDS, 'evidence'],
        'additionalProperties': False,
    }


def _normalize_evidence(value, page_count):
    """Keep usable model citations; these are navigation aids, not verified facts."""
    result = {field: [] for field in _EVIDENCE_FIELDS}
    if not isinstance(value, dict) or not page_count:
        return result
    aliases = {'author': 'authors', 'publisher': 'journal'}
    for key, citations in value.items():
        field = str(key).strip().lower()
        field = aliases.get(field, field)
        if field not in result or not isinstance(citations, list):
            continue
        for citation in citations:
            if not isinstance(citation, dict):
                continue
            page = citation.get('page')
            quote = citation.get('quote')
            if type(page) is not int or not 1 <= page <= page_count:
                continue
            if not isinstance(quote, str) or not 1 <= len(quote.strip()) <= 500:
                continue
            normalized = {'page': page, 'quote': quote.strip()}
            if normalized not in result[field]:
                result[field].append(normalized)
    return result


def get_metadata_from_snippet(pdf_bytes: bytes, is_book: bool, sdk_client=None, model=None) -> dict:
    sdk_client = sdk_client or client
    page_count = _snippet_page_count(pdf_bytes)
    upload_config = types.UploadFileConfig(display_name='snippet.pdf', mime_type='application/pdf')
    snippet_file = None
    try:
        snippet_file = sdk_client.files.upload(file=io.BytesIO(pdf_bytes), config=upload_config)
        document_instruction = (
            'This is a book: journal must contain the Publisher, not a journal name. '
            if is_book else 'This is an academic paper: journal is the journal name, if present. '
        )
        system_instruction = (
            'You are an academic document manager. '
            'Read all supplied pages and extract Authors, Year, Journal, Title. '
            + document_instruction +
            'If a field is NOT clearly present, return it EMPTY ("" or []); DO NOT GUESS or fabricate. '
            'Return strict JSON with authors (array of full names), year (string), journal (string), '
            'title (string), and evidence (object). Evidence has keys authors, year, journal, title, '
            'each holding an array of {"page": integer, "quote": string}. '
            'For each field, cite a short exact original text fragment visibly present in this PDF '
            'and its 1-based physical page position in the supplied PDF. Count from the first '
            'supplied page, ignoring printed page numbers, including Roman numerals. '
            'Metadata can occur on later supplied pages, not just the first page. '
            'Keep quotes in their original wording, spelling, and capitalization, at most 500 characters. '
            'Do not invent, paraphrase, or translate quotes. Do not fabricate page numbers. '
            'If you cannot locate evidence for a field, return an empty evidence array for that field. '
            'For books, cite the publisher under evidence.journal. '
            'An empty evidence array is allowed even when a metadata field was extracted.'
        )
        response = sdk_client.models.generate_content(
            model=model or MODEL_NAME,
            contents=[system_instruction, snippet_file],
            config=types.GenerateContentConfig(
                response_mime_type='application/json',
                response_json_schema=_metadata_output_schema(page_count),
                system_instruction=system_instruction,
            )
        )
        data = parse_metadata(response.text, page_count)
        evidence_raw = data['evidence']
    finally:
        if snippet_file is not None:
            snippet_name = getattr(snippet_file, 'name', None)
            if snippet_name:
                try:
                    try:
                        sdk_client.files.delete(name=snippet_name)
                    except TypeError:
                        sdk_client.files.delete(snippet_name)
                except Exception as e:
                    logging.warning(f"Failed to delete uploaded snippet '{snippet_name}': {e}")

    raw_year = _metadata_text(data.get('year'))
    year = 'n.d.' if (raw_year == '' or raw_year.lower() in {'unknown','unknownyear','n/a','na'}) else raw_year

    authors = _metadata_authors(data.get('authors') or data.get('author'))
    unknown_tokens = {'unknown','n/a','na','none','anonymous','unknown author','unknownauthors'}
    authors = [a for a in authors if a.strip() and a.strip().lower() not in unknown_tokens]
    authors = [titlecase(a) for a in authors]

    jraw = data.get('journal') or data.get('publisher')
    journal = _metadata_text(jraw)
    if journal.lower() in unknown_tokens:
        journal = ''
    journal = titlecase(journal) if journal else ''

    title = _metadata_text(data.get('title'))
    if title.lower() in unknown_tokens or title.lower() == 'unknowntitle':
        title = ''
    title = titlecase(title) if title else ''

    return {
        'authors': authors, 'year': year, 'journal': journal, 'title': title,
        'evidence': _normalize_evidence(evidence_raw, page_count),
    }

def _metadata_all_empty(meta: dict) -> bool:
    authors = meta.get('authors') or []
    authors_norm = [((a or '').strip().lower()) for a in authors]
    authors_empty = (len(authors_norm) == 0) or all(a == '' or a in ('unknownauthors', 'unknown author', 'unknown') for a in authors_norm)
    year = (meta.get('year') or '').strip().lower()
    year_empty = year in ('', 'n.d.', 'nd', 'unknown', 'unknownyear')
    journal_empty = ((meta.get('journal') or '').strip() == '')
    title_val = (meta.get('title') or '').strip()
    title_empty = (title_val == '' or title_val.lower() == 'unknowntitle')
    return authors_empty and year_empty and journal_empty and title_empty

# =========================
# Author formatting
# =========================
_SUFFIXES = {'jr', 'jr.', 'sr', 'sr.', 'ii', 'iii', 'iv'}

def _parse_author(full: str):
    if not full:
        return {'first':'', 'middle':'', 'surname':'', 'suffix':''}
    raw = re.sub(r'\s+', ' ', full).strip()
    comma_parts = [p.strip() for p in raw.split(',') if p.strip()]
    if len(comma_parts) >= 2 and comma_parts[1].lower() not in _SUFFIXES:
        surname = comma_parts[0]
        rest = re.sub(r'[;]', ' ', ' '.join(comma_parts[1:])).strip()
        parts = [p for p in rest.split() if p]
        suffix = ''
        if parts and parts[-1].lower() in _SUFFIXES:
            suffix = parts[-1]
            parts = parts[:-1]
        first = parts[0] if parts else ''
        middle = ' '.join(parts[1:]) if len(parts) > 1 else ''
        return {'first': first, 'middle': middle, 'surname': surname, 'suffix': suffix}

    s = re.sub(r'[;,]', ' ', raw).strip()
    parts = [p for p in s.split() if p]
    suffix = ''
    if parts and parts[-1].lower() in _SUFFIXES:
        suffix = parts[-1]; parts = parts[:-1]
    if not parts:
        return {'first':'', 'middle':'', 'surname':'', 'suffix':suffix}
    surname = parts[-1]
    if len(parts) == 1:
        return {'first':'', 'middle':'', 'surname':surname, 'suffix':suffix}
    first = parts[0]
    middle_parts = parts[1:-1]
    middle = ' '.join(middle_parts) if middle_parts else ''
    return {'first': first, 'middle': middle, 'surname': surname, 'suffix': suffix}

def _initial(s: str) -> str:
    return s[0].upper() if s else ''

def _middle_initials(m: str) -> str:
    if not m: return ''
    tokens = [t for t in m.split() if t]
    return ' '.join([_initial(t) + '.' for t in tokens])

def _render_author(fmt: str, comps: dict) -> str:
    tokens = {
        'first': comps.get('first', ''),
        'middle': comps.get('middle', ''),
        'surname': comps.get('surname', ''),
        'last': comps.get('surname', ''),
        'family': comps.get('surname', ''),
        'suffix': comps.get('suffix', ''),
        'first_initial': _initial(comps.get('first', '')),
        'surname_initial': _initial(comps.get('surname', '')),
        'middle_initials': _middle_initials(comps.get('middle', '')),
    }
    out = fmt
    for k, v in tokens.items():
        out = out.replace('{' + k + '}', v)
    out = re.sub(r'\s+', ' ', out).strip()
    out = re.sub(r'\s+,', ',', out); out = re.sub(r',\s*,', ',', out)
    out = re.sub(r'\(\s*\)', '', out); out = re.sub(r'\s+\.', '.', out)
    out = out.strip(' ,')
    return titlecase(out) if out else ''

def format_authors_list(authors_list, is_book: bool, author_format=None) -> str:
    fmt = author_format if author_format is not None else (AUTHOR_FMT_BOOK if is_book else AUTHOR_FMT_PAPER)
    if not authors_list:
        return 'UnknownAuthors'
    rendered = []
    for full in authors_list:
        comps = _parse_author(full)
        s = _render_author(fmt, comps)
        if s:
            rendered.append(s)
    return ', '.join(rendered) if rendered else 'UnknownAuthors'

# =========================
# Filename builder
# =========================
def build_new_filename(meta: dict, is_book: bool = False, naming_settings=None) -> str:
    naming = naming_settings or {}
    journal = meta.get('journal') or naming.get('unpublished', UNPUBLISHED_PLACEHOLDER)
    authors_str = format_authors_list(meta.get('authors', []), is_book, naming.get('author_format'))
    pattern = naming.get('pattern', BOOK_OUTPUT_PATTERN if is_book else OUTPUT_PATTERN)
    filename = pattern.format(
        journal=journal,
        year=meta.get('year','n.d.'),
        authors=authors_str,
        title=meta.get('title','UnknownTitle')
    )
    cleaned = ''.join(c for c in filename if c not in INVALID_FILENAME_CHARS)
    return ' '.join(cleaned.split()).strip()

# =========================
# Read-only preview extraction
# =========================
def prepare_preview(file_list, pages, is_book, sdk_client, model, cancelled, on_progress, review_dir=None,
                    cache=None, force=False):
    import uuid

    rows = []
    unique = {}
    for path in file_list:
        absolute = os.path.abspath(path)
        unique.setdefault(os.path.normcase(absolute), absolute)
    for index, path in enumerate(unique.values(), 1):
        if cancelled():
            break
        row = {
            'source': path, 'original_source': path, 'metadata': {}, 'selected': False,
            'fingerprint': '', 'status': 'Error', 'needs_review': True,
            'snippet_path': '', 'page_count': 0, 'is_book': bool(is_book),
            'analysis_attempted': True,
            'requested_pages': pages,
            'resume_action': 'extract',
        }
        context = None
        try:
            row['fingerprint'] = fingerprint_file(path)
            context = ExtractionCache.context(row['fingerprint'], pages, is_book, model or MODEL_NAME)
            cached = cache.get(context) if cache is not None and not force else None
            if cached is not None:
                row['metadata'], snippet, row['page_count'] = cached
                row['extraction_source'] = 'cache'
            else:
                snippet = extract_first_n_pages(path, pages)
                row['page_count'] = _snippet_page_count(snippet) or 0
                if not row['page_count']:
                    raise ValueError('PDF has no readable pages to extract.')
                row['extraction_source'] = 'model'
            if review_dir is not None:
                os.makedirs(review_dir, exist_ok=True)
                snapshot = os.path.join(os.path.abspath(review_dir), f'{uuid.uuid4().hex}.pdf')
                with open(snapshot, 'xb') as stream:
                    stream.write(snippet)
                row['snippet_path'] = snapshot
            if cached is None:
                if isinstance(sdk_client, _LazyGeminiClient):
                    sdk_client.ensure()
                row['metadata'] = get_metadata_from_snippet(snippet, is_book, sdk_client, model)
            if fingerprint_file(path) != row['fingerprint']:
                raise ValueError('File contents changed during extraction; retry with the current file.')
            if cache is not None:
                if cached is None:
                    cache.put(context, row['metadata'], snippet, row['page_count'])
                cache.record(context, path, 'cache_hit' if cached is not None else 'extracted')
            missing = []
            if not _metadata_text(row['metadata'].get('title')):
                missing.append('title')
            if not _metadata_authors(row['metadata'].get('authors')):
                missing.append('authors')
            if not re.fullmatch(r'[1-9]\d{3}', _metadata_text(row['metadata'].get('year'))):
                missing.append('year (four digits)')
            row['needs_review'] = bool(missing)
            row['selected'] = not missing
            row['status'] = 'Needs review' if missing else 'Extracted'
            if missing:
                row.pop('resume_action', None)
            else:
                row['resume_action'] = 'rename'
            row['reason'] = 'Missing required metadata: ' + ', '.join(missing) + '.' if missing else ''
            if missing:
                row['message'] = row['reason']
        except Exception as exc:
            if isinstance(sdk_client, _LazyGeminiClient) and sdk_client.error is not None:
                row['analysis_attempted'] = False
            if force:
                row['force_refresh'] = True
            if cache is not None and context is not None:
                try:
                    cache.record(context, path, 'failed', type(exc).__name__)
                except Exception:
                    logging.warning('Could not record extraction failure', exc_info=True)
            row['extraction_error'] = str(exc)
            row['message'] = str(exc)
            row['reason'] = 'Extraction failed: ' + str(exc)
        rows.append(row)
        on_progress(index, len(unique), row)
    return rows


def retry_failed_extractions(rows, pages, is_book, sdk_client, model, cancelled, on_progress,
                             review_dir=None):
    """Refresh failed rows in place, keeping the full batch and original paths."""
    failed = [row for row in rows if can_retry_extraction(row)]
    return extract_rows(failed, pages, is_book, sdk_client, model, cancelled, on_progress, review_dir)


def extract_rows(rows, pages, is_book, sdk_client, model, cancelled, on_progress, review_dir=None, cache=None):
    """Extract a selected subset of a durable manifest, preserving its file IDs."""
    retried = []
    for index, row in enumerate(rows, 1):
        if cancelled():
            break
        fresh = prepare_preview([row['source']], row.get('requested_pages', pages), is_book, sdk_client, model,
                                cancelled, lambda *args: None, review_dir=review_dir, cache=cache,
                                force=row.get('force_refresh', False))
        if not fresh:
            break
        original_source = row.get('original_source', row['source'])
        stale_keys = set(row) - set(fresh[0]) - {'original_source', 'row_id'}
        row.update(fresh[0], original_source=original_source)
        for key in stale_keys:
            row.pop(key, None)
        retried.append(row)
        on_progress(index, len(rows), row)
    return retried


def reanalyze_row(row, pages, is_book, sdk_client, model, cancelled, review_dir=None, cache=None):
    """Replace one result only after a successful fresh extraction; never rename."""
    if type(pages) is not int or not 1 <= pages <= MAX_PAGES_TO_EXTRACT:
        raise ValueError(f'Pages must be between 1 and {MAX_PAGES_TO_EXTRACT}.')
    if row.get('pending_move') or row.get('status') == 'Recovery needed':
        raise ValueError('Resolve the interrupted move before reanalyzing this file.')
    fresh = prepare_preview([row['source']], pages, is_book, sdk_client, model,
                            cancelled, lambda *args: None, review_dir, cache=cache, force=True)
    if not fresh:
        return False
    replacement = fresh[0]
    if replacement.get('extraction_error'):
        raise ValueError('Reanalysis failed; previous result kept. ' + replacement['extraction_error'])
    if fingerprint_file(row['source']) != replacement['fingerprint']:
        raise ValueError('File contents changed during reanalysis; previous result kept.')
    replacement['original_source'] = row.get('original_source', row['source'])
    if 'row_id' in row:
        replacement['row_id'] = row['row_id']
    replacement.update(status='Needs review', needs_review=True, selected=False,
                       message='Reanalysis completed. Review the new metadata and apply a correction when ready.')
    replacement.pop('resume_action', None)
    row.clear()
    row.update(replacement)
    return True

# =========================
# Main GUI
# =========================
class _LazyGeminiClient:
    """Allow cache-only batches without credentials or a provider connection."""
    def __init__(self, api_key):
        self.api_key = api_key
        self.instance = None
        self.error = None

    def ensure(self):
        if self.error is not None:
            raise self.error
        if self.instance is None:
            try:
                if not self.api_key:
                    raise ValueError('Set your Gemini API key in Config → Settings for uncached files.')
                self.instance = genai.Client(api_key=self.api_key)
            except Exception as exc:
                self.error = exc
                raise
        return self.instance

    def __getattr__(self, name):
        return getattr(self.ensure(), name)

    def close(self):
        if self.instance is not None:
            self.instance.close()

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
                  'unpublished': UNPUBLISHED_PLACEHOLDER}
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
