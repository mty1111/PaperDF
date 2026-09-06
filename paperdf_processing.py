"""Automatic batch policy and local corrections, independent of Tk and AI."""
import os

from paperdf_workflow import apply_batch, fingerprint_file, plan_renames


def _key(path):
    return os.path.normcase(os.path.abspath(path))


def can_retry_extraction(row):
    """Only extraction errors need another model request, not local review issues."""
    return row.get('status') == 'Error' and 'extraction_error' in row


def record_results(rows, results):
    """Track current paths so review/undo can continue after a rename."""
    by_source = {_key(row['source']): row for row in rows}
    for result in results:
        row = by_source.get(_key(result.source))
        if row is None and result.status == 'restored':
            row = next((item for item in rows if
                        _key(item.get('pending_move', {}).get('destination', item['source'])) ==
                        _key(result.source)), None)
        if row is None:
            continue
        row['message'] = result.message
        if result.status in ('renamed', 'restored'):
            row.setdefault('original_source', row['source'])
            row['source'] = result.destination
            row['display_name'] = os.path.basename(result.destination)
            row['status'] = 'Renamed' if result.status == 'renamed' else 'Undone'
            row['needs_review'] = False
            row.pop('suggested_name', None)
            row.pop('reason', None)
            row.pop('extraction_error', None)
            row.pop('resume_action', None)
            row.pop('pending_move', None)
            row.pop('validation_previous', None)
        elif result.status == 'failed':
            row['status'], row['needs_review'] = 'Failed', True
        else:
            row['status'], row['needs_review'] = 'Stopped', True


def plan_rows(rows, build_name, should_stop=None):
    """Plan automatic-policy results without moving files or writing journals."""
    requests, expected = [], {}
    for row in rows:
        row.setdefault('original_source', row['source'])
        if row.get('needs_review', not row.get('selected', False)):
            continue
        try:
            name = row.get('override') or build_name(row['metadata'])
            requests.append((row['source'], name))
            expected[row['source']] = row['fingerprint']
        except Exception as exc:
            row.update(status='Needs review', needs_review=True, message=str(exc))
            row.pop('resume_action', None)
    by_source = {_key(row['source']): row for row in rows}
    plans = plan_renames(requests, expected, should_stop=should_stop)
    ready, duplicates = [], []
    for plan in plans:
        row = by_source[_key(plan.source)]
        row['display_name'] = os.path.basename(plan.destination) if plan.destination else plan.proposed_name
        row['message'] = plan.message
        if plan.status == 'ready':
            exact = os.path.join(os.path.dirname(plan.source), plan.proposed_name)
            if _key(plan.destination) != _key(exact):
                row.update(status='Needs review', needs_review=True,
                           suggested_name=os.path.basename(plan.destination),
                           message='Filename conflict. Review this document and choose a distinct name.')
                row.pop('resume_action', None)
            else:
                row.update(status='Ready', needs_review=False, resume_action='rename')
                ready.append(plan)
        elif plan.status == 'same':
            row.update(status='Unchanged', needs_review=False)
            row.pop('resume_action', None)
        elif plan.status == 'duplicate':
            row.update(status='Duplicate', needs_review=False)
            row.pop('resume_action', None)
            duplicates.append((row, plan.destination))
        else:
            row.update(status='Stopped' if plan.status == 'cancelled' else 'Failed', needs_review=True,
                       resume_action='rename')

    return ready, duplicates


def process_rows(rows, build_name, journal_dir, should_stop=None, on_result=None, before_move=None):
    """Rename complete, unambiguous rows as one undoable batch.

    Missing fields and name collisions remain in the results for optional review.
    This is a mechanical completeness check, not a confidence estimate.
    """
    ready, duplicates = plan_rows(rows, build_name, should_stop)

    def report(result):
        record_results(rows, [result])
        if on_result:
            on_result(result)

    try:
        return apply_batch(ready, journal_dir, should_stop=should_stop, on_result=report, before_move=before_move)
    finally:
        # A planned duplicate is real only if the matching target was actually
        # created. Deferred collisions, cancellation or failures may leave it absent.
        for row, destination in duplicates:
            try:
                materialized = fingerprint_file(destination) == row['fingerprint']
            except (OSError, ValueError):
                materialized = False
            if not materialized:
                row.update(status='Needs review', needs_review=True,
                           suggested_name=os.path.basename(destination),
                           message='The matching target file was not created. Review this document before renaming.')


def correct_row(row, metadata, override, build_name, journal_dir, should_stop=None, on_result=None, before_move=None):
    """Apply a user's local correction to the current file, with a new undo batch."""
    if not row.get('fingerprint'):
        raise ValueError('The original file could not be read. Process it again before changing its name.')
    if row.get('pending_move'):
        raise ValueError('An interrupted move needs recovery. Continue batch to check it, or use Undo last batch.')
    if not override and not any(metadata.get(key) for key in ('authors', 'journal', 'title')):
        raise ValueError('Enter document metadata or a filename ending in .pdf.')
    row['metadata'], row['override'] = metadata, override
    row.pop('reason', None)
    row.pop('extraction_error', None)
    name = override or build_name(metadata)
    plan = plan_renames([(row['source'], name)], {row['source']: row['fingerprint']}, should_stop=should_stop)[0]
    row['display_name'] = os.path.basename(plan.destination) if plan.destination else name
    if plan.status == 'ready':
        if _key(plan.destination) != _key(os.path.join(os.path.dirname(plan.source), name)):
            row.update(status='Needs review', needs_review=True, suggested_name=os.path.basename(plan.destination),
                       message='That filename is occupied. Review again to use the suggested distinct name or enter another.')
            row.pop('resume_action', None)
            return None

        def report(result):
            record_results([row], [result])
            if on_result:
                on_result(result)

        row['resume_action'] = 'rename'
        return apply_batch([plan], journal_dir, should_stop, report, before_move)
    row['message'] = plan.message
    row['status'] = {'same': 'Unchanged', 'duplicate': 'Duplicate', 'cancelled': 'Stopped'}.get(plan.status, 'Failed')
    row['needs_review'] = plan.status not in ('same', 'duplicate')
    if row['needs_review']:
        row['resume_action'] = 'rename'
    else:
        row.pop('resume_action', None)
    if not row['needs_review']:
        row.pop('suggested_name', None)
    return None
