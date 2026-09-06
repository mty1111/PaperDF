"""Gemini adapter: upload, strict extraction contract and response normalization."""
import io
import logging
from PyPDF2 import PdfReader
from google import genai
from google.genai import types
from paperdf_defaults import DEFAULT_MODEL
from paperdf_schema import parse_metadata
from paperdf_academic import AUTHOR_KINDS, DOCUMENT_KINDS, DATE_KINDS, compact, select_year

_EVIDENCE_FIELDS = ('authors', 'year', 'journal', 'title')

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
            'academic': {
                'type': 'object',
                'properties': {
                    'document_kind': {'type': 'string', 'enum': list(DOCUMENT_KINDS)},
                    'author_details': {'type': 'array', 'items': {
                        'type': 'object', 'properties': {
                            'literal': {'type': 'string'}, 'kind': {'type': 'string', 'enum': list(AUTHOR_KINDS)},
                            'given': {'type': 'string'}, 'family': {'type': 'string'}, 'suffix': {'type': 'string'}},
                        'required': ['literal', 'kind', 'given', 'family', 'suffix'], 'additionalProperties': False}},
                    'dates': {'type': 'array', 'items': {
                        'type': 'object', 'properties': {
                            'kind': {'type': 'string', 'enum': list(DATE_KINDS)}, 'year': {'type': 'string'},
                            'page': page_schema, 'quote': {'type': 'string', 'description': 'Exact date passage, 1-500 characters'}},
                        'required': ['kind', 'year', 'page', 'quote'], 'additionalProperties': False}}},
                'required': ['document_kind', 'author_details', 'dates'], 'additionalProperties': False},
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
        'required': [*_EVIDENCE_FIELDS, 'evidence', 'academic'],
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
    if sdk_client is None:
        raise ValueError("A Gemini client is required for extraction.")
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
            'An empty evidence array is allowed even when a metadata field was extracted. '
            'Also return academic with document_kind, author_details and dates. '
            'Preserve source capitalization, accents, hyphens and acronyms in names, title and journal. '
            'author_details aligns one-to-one with authors: literal is the same full name; kind is person, '
            'organization or unknown. For a person give given, family (including every surname particle '
            'and compound family-name component) and suffix. Never shorten an institutional author. '
            'For organizations and unknown authors leave given/family/suffix empty. If a personal name '
            'cannot be split reliably, leave family empty instead of guessing. Affiliations are not authors. '
            'document_kind is published_article, preprint, book or unknown for this actual document. '
            'dates lists dates explicitly describing this document/version, never references: kind is '
            'publication, online, revision, preprint, copyright, original, received, accepted, accessed '
            'or other; give year (four digits), physical page and exact quote. Publication for a book '
            'means the present edition, not an earlier original edition. Distinguish posted revisions '
            'from journal publication, received/accepted dates and download/access stamps. '
            'Do not infer publication merely from a journal name, DOI, affiliation or reference. '
            'Return an empty dates array if no date is supported by a visible passage.'
        )
        response = sdk_client.models.generate_content(
            model=model or DEFAULT_MODEL,
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
    authors = [compact(a) for a in authors]

    jraw = data.get('journal') or data.get('publisher')
    journal = _metadata_text(jraw)
    if journal.lower() in unknown_tokens:
        journal = ''
    journal = compact(journal)

    title = _metadata_text(data.get('title'))
    if title.lower() in unknown_tokens or title.lower() == 'unknowntitle':
        title = ''
    title = compact(title)
    academic = data['academic']
    academic['author_details'] = [item for item in academic['author_details'] if compact(item['literal']) in authors]
    year = select_year(academic)[0] or 'n.d.'

    return {
        'authors': authors, 'year': year, 'journal': journal, 'title': title,
        'evidence': _normalize_evidence(evidence_raw, page_count), 'academic': academic,
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
                    raise ValueError('Set a Gemini API key in Settings or GEMINI_API_KEY for uncached files.')
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


