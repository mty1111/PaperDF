# pdf_metadata_renamer.py — PaperDF edition
# Features:
# - App branding via APP_NAME = "PaperDF"
# - Separate author-format options for papers vs. books
# - Default model: gemini-2.5-flash-lite
# - Default book template: {authors} - {title} - {journal} ({year}).pdf
# - Embedded Cheatsheet inside Settings (no separate menu)
# - Expanded Help with full user guide
# - First-run setup guide popup when no config file exists
# - LLM-assisted strict filename "already-formatted" check
# - Duplicate-content detection (hash) and safe renaming
# - App/window icon support (dev + PyInstaller onefile)

import os
import io
import sys
import logging
import json
import threading
import hashlib
import configparser
import re
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from tkinter import PhotoImage

from dotenv import load_dotenv
from PyPDF2 import PdfReader, PdfWriter

# Google AI Studio SDK (pip install google-genai)
from google import genai
from google.genai import types

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
        "© 2025 PaperDF contributors\n"
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
- The model returns strictly structured metadata: Authors[], Year, Journal, Title.
  • For books, Journal is interpreted as the Publisher.
- The app builds a clean filename from your templates and renames the file safely.

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
   • If your files have longer prefaces or title pages, increase this value.

4) Start / Abort
   • Click “Start” to process, “Abort” to stop the current run.
   • The log shows every decision: skips, outputs, and errors.

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
A) Already-formatted check:
   The app uses a strict validator to decide if a filename already matches the current pattern
   (including author-format rules). If yes → “Already formatted—skipped”.

B) Metadata extraction:
   The app reads the first N pages and asks the model to return strict JSON with:
     authors (array), year (string), journal (string), title (string).
   • Empty/unknown values are cleaned (e.g., year → "n.d." if absent).
   • “Journal” is title-cased; for books it is treated as the publisher.

C) Filename building:
   • {{authors}} is built by parsing each name into parts (first/middle/surname/suffix)
     and rendering them with your author-format template, then joining with ", ".
   • The final filename is created via your Output Pattern / Book Output Pattern.
   • Illegal filesystem characters are stripped; whitespace is normalized.

D) Collisions & duplicates:
   • If the new name equals the current name → “Skipped (same name)”.
   • If a file with the target name exists:
       - If contents match (SHA-1) → “Duplicate content detected; skipped rename”.
       - Else the app appends a short hash (e.g., “[1a2b3c4d]”) to make a unique name.

E) Skips:
   • Empty metadata (nothing useful extracted) → skipped.
   • Book mode but no title found → skipped (to avoid meaningless names).

First run:
- If no config file exists, a Setup Guide pops up automatically.
  Use it to open Settings, paste API key, and review templates.

Privacy & scope:
- Only the first N pages are uploaded to the model to minimize data exposure.
- No raw PDFs are stored by the app; renaming is local.
- Name parsing is heuristic and may need manual edits for unusual name orders or capitalization.

Troubleshooting:
- “Gemini API key is required” → Set your key in Settings.
- “Invalid JSON” from model → Increase pages to extract, verify the PDF has metadata on early pages.
- Repeated “Skipped (same name)” → Your template currently evaluates to the existing filename.
- Wrong author format → Adjust the Author Format fields in Settings; use tokens correctly.
- Unexpected publisher/journal → For books, {{journal}} is the publisher by design.

Config & storage:
- Config file: created under your user directory (pdf_metadata_renamer.config).
- ENV file (optional API key): pdf_metadata_renamer.env in the same folder.
- Both are created in the app’s storage directory shown by your OS (LOCALAPPDATA on Windows; home on others).

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
    reader = PdfReader(pdf_path)
    writer = PdfWriter()
    for page in reader.pages[:n]:
        writer.add_page(page)
    buf = io.BytesIO()
    writer.write(buf)
    buf.seek(0)
    return buf.read()

# =========================
# Gemini metadata extraction
# =========================
def get_metadata_from_snippet(pdf_bytes: bytes, is_book: bool) -> dict:
    global client
    upload_config = types.UploadFileConfig(display_name='snippet.pdf', mime_type='application/pdf')
    snippet_file = client.files.upload(file=io.BytesIO(pdf_bytes), config=upload_config)
    system_instruction = (
        'You are an academic document manager. '
        'From the first pages of the provided PDF, extract ONLY these fields: '
        'Authors, Year, Journal, Title. For books, Journal should be the Publisher. '
        'If a field is NOT clearly present, return it EMPTY ("" or []); DO NOT GUESS or fabricate. '
        'Return strict JSON with keys: authors (array), year (string), journal (string), title (string).'
    )
    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=[system_instruction, snippet_file],
        config=types.GenerateContentConfig(response_mime_type='application/json', system_instruction=system_instruction)
    )
    logging.info(f"Gemini raw response: {response.text}")
    try:
        data_raw = json.loads(response.text)
        data = {k.lower(): v for k, v in data_raw.items()}
    except json.JSONDecodeError:
        raise ValueError(f'Invalid JSON: {response.text}')

    raw_year = data.get('year')
    year = 'n.d.' if (raw_year is None or str(raw_year).strip() == '' or str(raw_year).strip().lower() in {'unknown','unknownyear','n/a','na'}) else str(raw_year).strip()

    authors = data.get('authors') or []
    if isinstance(authors, str):
        authors = [a.strip() for a in authors.split(',') if a.strip()]
    unknown_tokens = {'unknown','n/a','na','none','anonymous','unknown author','unknownauthors'}
    authors = [a for a in authors if a.strip() and a.strip().lower() not in unknown_tokens]
    authors = [titlecase(a) for a in authors]

    jraw = data.get('journal')
    journal = (jraw or '').strip()
    if journal.lower() in unknown_tokens:
        journal = ''
    journal = titlecase(journal) if journal else ''

    traw = data.get('title')
    title = (traw or '').strip()
    if title.lower() in unknown_tokens or title.lower() == 'unknowntitle':
        title = ''
    title = titlecase(title) if title else ''

    return {'authors': authors, 'year': year, 'journal': journal, 'title': title}

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
_SUFFIXES = {'jr', 'jr.', 'sr.', 'ii', 'iii', 'iv'}

def _parse_author(full: str):
    if not full:
        return {'first':'', 'middle':'', 'surname':'', 'suffix':''}
    s = re.sub(r'[;,]', ' ', full).strip()
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

def format_authors_list(authors_list, is_book: bool) -> str:
    fmt = AUTHOR_FMT_BOOK if is_book else AUTHOR_FMT_PAPER
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
def build_new_filename(meta: dict, is_book: bool = False) -> str:
    journal = meta.get('journal') or UNPUBLISHED_PLACEHOLDER
    authors_str = format_authors_list(meta.get('authors', []), is_book)
    pattern = BOOK_OUTPUT_PATTERN if is_book else OUTPUT_PATTERN
    filename = pattern.format(
        journal=journal,
        year=meta.get('year','n.d.'),
        authors=authors_str,
        title=meta.get('title','UnknownTitle')
    )
    cleaned = ''.join(c for c in filename if c not in INVALID_FILENAME_CHARS)
    return ' '.join(cleaned.split()).strip()

# =========================
# Content hash & duplicates
# =========================
def _sha1_hex(path: str, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(chunk_size), b''):
            h.update(chunk)
    return h.hexdigest()

def _same_file(a: str, b: str) -> bool:
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        return _sha1_hex(a) == _sha1_hex(b)
    except Exception:
        return False

# =========================
# Filename format validator (LLM-assisted)
# =========================
def filename_already_formatted(file_path: str, is_book: bool) -> bool:
    """
    Ask the model to validate if the base filename already conforms to the current pattern
    (including author-format rules). Returns True to skip renaming.
    """
    global client
    name = os.path.basename(file_path)
    pattern = BOOK_OUTPUT_PATTERN if is_book else OUTPUT_PATTERN
    mode = 'book' if is_book else 'paper'
    rules = (
        "For paper mode: authors must follow the configured author format; "
        "for book mode: authors must follow its configured format. "
        "Use the given pattern as the canonical order and separators. "
        "For books, {journal} stands for the publisher. "
        "Ignore directory paths; check only the base filename (without leading folders)."
    )
    system_instruction = (
        "You are a strict filename validator. Given a filename and an expected pattern with placeholders "
        "{journal}, {year}, {authors}, {title}, answer whether the filename already conforms to the pattern and style. "
        'Return ONLY JSON as {"ok": true} or {"ok": false}.'
    )
    prompt = (
        f"Filename: {name}\nMode: {mode}\nExpected pattern: {pattern}\nRules: {rules}\n"
        "Decide if the filename is already correctly formatted."
    )
    try:
        resp = client.models.generate_content(
            model=MODEL_NAME,
            contents=[prompt],
            config=types.GenerateContentConfig(response_mime_type='application/json', system_instruction=system_instruction)
        )
        data = json.loads(resp.text)
        if isinstance(data, dict):
            val = data.get('ok')
            if isinstance(val, bool):
                return val
            if isinstance(val, str):
                return val.strip().lower() in ('true', 'yes', 'y', '1')
    except Exception as e:
        logging.warning(f"Filename check failed for '{name}': {e}")
    return False

# =========================
# Processing
# =========================
def process_list(file_list, pages, is_book, log_widget, progress_bar):
    global client
    if not API_KEY:
        messagebox.showerror('Error', 'Gemini API key is required.')
        return
    client = genai.Client(api_key=API_KEY)
    log_widget.delete('1.0', tk.END)
    stop_event.clear()
    total = len(file_list)
    progress_bar.config(maximum=total, value=0)
    for idx, path in enumerate(file_list, 1):
        if stop_event.is_set():
            log_widget.insert(tk.END, 'Aborted by user.\n'); break
        log_widget.insert(tk.END, f'Processing ({idx}/{total}): {path}\n')
        try:
            if filename_already_formatted(path, is_book):
                log_widget.insert(tk.END, 'Already formatted—skipped\n')
                progress_bar['value'] = idx; log_widget.see(tk.END); continue
            snippet = extract_first_n_pages(path, pages)
            meta = get_metadata_from_snippet(snippet, is_book)
            if is_book and not (meta.get('title') or '').strip():
                log_widget.insert(tk.END, 'Skipped (book title not found)\n')
                progress_bar['value'] = idx; log_widget.see(tk.END); continue
            if _metadata_all_empty(meta):
                log_widget.insert(tk.END, 'Skipped (empty metadata)\n')
                progress_bar['value'] = idx; log_widget.see(tk.END); continue
            new_name = build_new_filename(meta, is_book)
            dir_path = os.path.dirname(path)
            dst = os.path.join(dir_path, new_name)
            if os.path.abspath(path) == os.path.abspath(dst):
                log_widget.insert(tk.END, 'Skipped (same name)\n'); continue
            if os.path.exists(dst):
                try:
                    if _same_file(path, dst):
                        log_widget.insert(tk.END, 'Duplicate content detected; skipped rename\n'); continue
                except Exception:
                    pass
                base, ext = os.path.splitext(new_name)
                short = _sha1_hex(path)[:8]
                candidate = f"{base} [{short}]{ext}"
                dst = os.path.join(dir_path, candidate)
                counter = 2
                while os.path.exists(dst):
                    try:
                        if _same_file(path, dst):
                            log_widget.insert(tk.END, 'Duplicate content detected; skipped rename'); break
                    except Exception:
                        pass
                    candidate = f"{base} [{short}-{counter}]{ext}"
                    dst = os.path.join(dir_path, candidate); counter += 1
                if os.path.exists(dst) and _same_file(path, dst):
                    continue
            log_widget.insert(tk.END, f'Input: {os.path.basename(path)}\n')
            os.replace(path, dst)
            log_widget.insert(tk.END, f'Output: {os.path.basename(dst)}\n')
        except Exception as e:
            log_widget.insert(tk.END, f'Error: {e}\n')
        progress_bar['value'] = idx; log_widget.see(tk.END)
    if not stop_event.is_set():
        log_widget.insert(tk.END, 'Done!\n')
    progress_bar.stop()

# =========================
# Main GUI
# =========================
def main():
    global API_KEY, selected_files, files_entry, folder_entry
    selected_files = []
    root = tk.Tk()
    root.title(f"{APP_NAME} {APP_VERSION}")

    # --- App/window icon (cross-platform) ---
    try:
        icon_png = resource_path("assets/icon.png")
        if os.path.exists(icon_png):
            # keep reference to avoid garbage collection
            root._icon_image = PhotoImage(file=icon_png)
            root.wm_iconphoto(True, root._icon_image)
    except Exception:
        ico_path = resource_path("assets/icon.ico")
        if os.path.exists(ico_path):
            try:
                root.iconbitmap(ico_path)
            except Exception:
                pass
    # ----------------------------------------

    menubar = tk.Menu(root)

    # Config menu (Help lives here; Cheatsheet is embedded in Settings)
    cfg = tk.Menu(menubar, tearoff=0)
    cfg.add_command(label='Settings...', command=show_config)
    cfg.add_separator()
    cfg.add_command(label='Help...', command=show_help_config)
    cfg.add_command(label='About...', command=lambda: show_about_dialog(root))
    cfg.add_separator()
    cfg.add_command(label=f'Version: {APP_VERSION}', state='disabled')
    menubar.add_cascade(label='Config', menu=cfg)


    root.config(menu=menubar)

    # Layout
    root.grid_columnconfigure(1, weight=1)
    root.grid_rowconfigure(4, weight=1)

    tk.Label(root, text='PDF Folder:').grid(row=1, column=0, sticky='e', padx=5, pady=5)
    fld = tk.Entry(root)
    fld.grid(row=1, column=1, sticky='we', padx=5)
    global folder_entry; folder_entry = fld
    tk.Button(root, text='Browse Folder', command=lambda: browse_folder(fld)).grid(row=1, column=2, padx=5, pady=5)

    tk.Label(root, text='Or select PDF Files:').grid(row=2, column=0, sticky='e', padx=5, pady=5)
    files_entry_local = tk.Entry(root, state='readonly')
    files_entry_local.grid(row=2, column=1, sticky='we', padx=5)
    global files_entry; files_entry = files_entry_local
    tk.Button(root, text='Browse Files', command=select_files).grid(row=2, column=2, padx=5, pady=5)

    tk.Label(root, text='Pages to extract:').grid(row=3, column=0, sticky='e', padx=5, pady=5)
    pge = tk.Entry(root, width=5)
    pge.insert(0, str(DEFAULT_PAPER_PAGES))
    pge.grid(row=3, column=1, sticky='w', padx=5, pady=5)

    # Book mode toggle updates default pages
    book_var = tk.BooleanVar(value=False)
    def on_toggle_book():
        pge.delete(0, tk.END)
        pge.insert(0, str(DEFAULT_BOOK_PAGES if book_var.get() else DEFAULT_PAPER_PAGES))
    tk.Checkbutton(root, text='Book mode', variable=book_var, command=on_toggle_book).grid(row=3, column=2, padx=5, pady=5)

    tk.Label(root, text='Log:').grid(row=4, column=0, sticky='nw', padx=5, pady=5)
    logw = scrolledtext.ScrolledText(root)
    logw.grid(row=4, column=1, columnspan=2, sticky='nsew', padx=5, pady=5)

    prog = ttk.Progressbar(root, orient='horizontal', mode='determinate')
    prog.grid(row=5, column=1, columnspan=2, sticky='we', padx=5, pady=5)

    def on_start():
        folder = fld.get().strip()
        if selected_files and folder:
            messagebox.showerror('Error', 'Choose folder or files, not both.'); return
        if not selected_files and not folder:
            messagebox.showerror('Error', 'Select files or folder.'); return
        try:
            pages = int(pge.get().strip())
        except:
            pages = DEFAULT_PAPER_PAGES
        items = selected_files if selected_files else [
            os.path.join(dp, f) for dp, _, fs in os.walk(folder) for f in fs if f.lower().endswith('.pdf')
        ]
        if not items:
            messagebox.showinfo('Info', 'No PDF files found.'); return
        start_btn.config(state='disabled'); abort_btn.config(state='normal')
        threading.Thread(
            target=lambda: (process_list(items, pages, book_var.get(), logw, prog),
                            start_btn.config(state='normal'),
                            abort_btn.config(state='disabled')),
            daemon=True
        ).start()

    def on_abort():
        stop_event.set()

    start_btn = tk.Button(root, text='Start', command=on_start)
    start_btn.grid(row=6, column=1, sticky='w', padx=5, pady=10)

    abort_btn = tk.Button(root, text='Abort', command=on_abort, state='disabled')
    abort_btn.grid(row=6, column=2, padx=5, pady=10)

    tk.Button(root, text='Exit', command=root.destroy).grid(row=6, column=3, sticky='e', padx=5, pady=10)

    # First-run setup guide appears if no config exists
    if FIRST_RUN:
        root.after(200, lambda: show_setup_guide(root))

    root.mainloop()

# =========================
# Entrypoint
# =========================
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    main()
