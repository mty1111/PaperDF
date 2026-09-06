"""Headless PDF extraction orchestration shared by GUI and CLI.

Callbacks carry progress and cancellation; no Tk, user config or GUI imports.
The extractor argument provides the provider boundary (currently Gemini).
"""
import io
import os
import re
import logging
from PyPDF2 import PdfReader, PdfWriter
from paperdf_defaults import DEFAULT_MODEL, MAX_PAGES_TO_EXTRACT
from paperdf_gemini import (_metadata_text, _metadata_authors, _snippet_page_count,
                            get_metadata_from_snippet, _LazyGeminiClient)
from paperdf_academic import academic_review_reasons
from paperdf_cache import ExtractionCache
from paperdf_processing import can_retry_extraction
from paperdf_workflow import fingerprint_file

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


def prepare_preview(file_list, pages, is_book, sdk_client, model, cancelled, on_progress, review_dir=None,
                    cache=None, force=False, *, extractor=None):
    import uuid

    extractor = extractor or get_metadata_from_snippet

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
            context = ExtractionCache.context(row['fingerprint'], pages, is_book, model or DEFAULT_MODEL)
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
                row['metadata'] = extractor(snippet, is_book, sdk_client, model)
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
            academic_issues = academic_review_reasons(row['metadata'])
            row['needs_review'] = bool(missing or academic_issues)
            row['selected'] = not row['needs_review']
            row['status'] = 'Needs review' if row['needs_review'] else 'Extracted'
            if row['needs_review']:
                row.pop('resume_action', None)
            else:
                row['resume_action'] = 'rename'
            row['reason'] = 'Missing required metadata: ' + ', '.join(missing) + '.' if missing else ''
            row['reason'] = ' '.join(filter(None, [row['reason'], *academic_issues]))
            if row['needs_review']:
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


def extract_rows(rows, pages, is_book, sdk_client, model, cancelled, on_progress, review_dir=None, cache=None, *, preview=None):
    """Extract a selected subset of a durable manifest, preserving its file IDs."""
    preview = preview or prepare_preview
    retried = []
    for index, row in enumerate(rows, 1):
        if cancelled():
            break
        fresh = preview([row['source']], row.get('requested_pages', pages), is_book, sdk_client, model,
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


def reanalyze_row(row, pages, is_book, sdk_client, model, cancelled, review_dir=None, cache=None, *, preview=None):
    """Replace one result only after a successful fresh extraction; never rename."""
    preview = preview or prepare_preview
    if type(pages) is not int or not 1 <= pages <= MAX_PAGES_TO_EXTRACT:
        raise ValueError(f'Pages must be between 1 and {MAX_PAGES_TO_EXTRACT}.')
    if row.get('pending_move') or row.get('status') == 'Recovery needed':
        raise ValueError('Resolve the interrupted move before reanalyzing this file.')
    fresh = preview([row['source']], pages, is_book, sdk_client, model,
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
