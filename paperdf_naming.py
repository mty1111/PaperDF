"""Pure filename construction shared by GUI and CLI."""
import re
from paperdf_academic import parse_name, author_components, compact, canonical_journal, title_style, DEFAULT_ALIASES
from paperdf_defaults import (INVALID_FILENAME_CHARS, DEFAULT_OUTPUT_PATTERN, DEFAULT_BOOK_OUTPUT_PATTERN,
                              DEFAULT_UNPUBLISHED, DEFAULT_AUTHOR_FMT_PAPER, DEFAULT_AUTHOR_FMT_BOOK)

def _parse_author(full: str):
    return parse_name(full)


def _initial(s: str) -> str:
    return '.-'.join(part[0].upper() for part in s.split('-') if part)


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
    return out


def format_authors_list(authors_list, is_book: bool, author_format=None, author_details=None) -> str:
    fmt = author_format if author_format is not None else (DEFAULT_AUTHOR_FMT_BOOK if is_book else DEFAULT_AUTHOR_FMT_PAPER)
    if not authors_list:
        return 'UnknownAuthors'
    rendered = []
    for full in authors_list:
        comps = author_components(full, author_details or [])
        s = compact(full) if comps is None else _render_author(fmt, comps)
        if s:
            rendered.append(s)
    return ', '.join(rendered) if rendered else 'UnknownAuthors'


def build_new_filename(meta: dict, is_book: bool = False, naming_settings=None) -> str:
    naming = naming_settings or {}
    aliases = naming.get('journal_aliases', '' if naming_settings is not None else DEFAULT_ALIASES)
    style = naming.get('title_style', 'preserve')
    journal = canonical_journal(meta.get('journal', ''), aliases, is_book)
    journal = journal or naming.get('unpublished', DEFAULT_UNPUBLISHED)
    authors_str = format_authors_list(meta.get('authors', []), is_book, naming.get('author_format'), meta.get('academic', {}).get('author_details'))
    pattern = naming.get('pattern', DEFAULT_BOOK_OUTPUT_PATTERN if is_book else DEFAULT_OUTPUT_PATTERN)
    filename = pattern.format(
        journal=journal,
        year=meta.get('year','n.d.'),
        authors=authors_str,
        title=title_style(meta.get('title','UnknownTitle'), style)
    )
    cleaned = ''.join(c for c in filename if c not in INVALID_FILENAME_CHARS)
    return ' '.join(cleaned.split()).strip()
