"""Export a batch's current results as an Excel-readable CSV report."""
import csv
import os
from pathlib import Path
import tempfile


COLUMNS = ('original_path', 'current_path', 'title', 'authors', 'year',
           'journal_or_publisher', 'status', 'needs_attention',
           'requested_pages', 'analyzed_pages', 'details')


def _cell(value):
    text = '' if value is None else str(value)
    # Model output and filenames are data, even when a spreadsheet recognizes
    # their first non-whitespace character as a formula prefix.
    if text.lstrip().startswith(('=', '+', '-', '@')) or text.startswith(('\t', '\r', '\n')):
        return "'" + text
    return text


def export_results(rows, destination):
    """Write all supplied rows atomically; never alter the batch or its PDFs."""
    target = Path(destination)
    if target.suffix.lower() != '.csv':
        raise ValueError('Choose a filename ending in .csv for the report.')
    count = 0
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8-sig', newline='',
                                         dir=target.parent, prefix='.paperdf-report-',
                                         suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            writer = csv.writer(stream)
            writer.writerow(COLUMNS)
            for row in rows:
                metadata = row.get('metadata') or {}
                values = (row.get('original_source', row.get('source', '')), row.get('source', ''),
                          metadata.get('title', ''), '\n'.join(metadata.get('authors') or []),
                          metadata.get('year', ''), metadata.get('journal', ''), row.get('status', ''),
                          'yes' if row.get('needs_review') else 'no', row.get('requested_pages', ''),
                          row.get('page_count', ''),
                          row.get('message') or row.get('reason') or row.get('extraction_error', ''))
                writer.writerow([_cell(value) for value in values])
                count += 1
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        return count
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
