"""Atomic checkpoints for the latest batch, including interrupted file moves."""
import copy
import json
import os
from pathlib import Path
import re
import shutil
import uuid

from paperdf_workflow import fingerprint_file, _save_journal, _sync_directory, _validate_pair


class SessionError(RuntimeError):
    pass


def can_continue(row):
    return (row.get('resume_action') in ('extract', 'rename') or
            row.get('status') in ('Changed', 'Missing', 'Unverified', 'Recovery needed'))


def pending_rows(paths, is_book):
    unique = {}
    for path in paths:
        absolute = os.path.abspath(path)
        unique.setdefault(os.path.normcase(absolute), absolute)
    return [{'row_id': uuid.uuid4().hex, 'source': path, 'original_source': path,
             'metadata': {}, 'fingerprint': '', 'status': 'Pending', 'selected': False,
             'needs_review': True, 'resume_action': 'extract', 'analysis_attempted': False,
             'snippet_path': '', 'page_count': 0, 'is_book': is_book}
            for path in unique.values()]


def remember_move(rows, result, action):
    for row in rows:
        if (os.path.normcase(row['source']) == os.path.normcase(result.source) or
                (action == 'undo' and row.get('pending_move', {}).get('destination') == result.source)):
            row['source'] = result.source
            row['pending_move'] = {'source': result.source, 'destination': result.destination,
                                   'action': action, 'previous_status': row.get('status', 'Renamed'),
                                   'previous_needs_review': row.get('needs_review', False)}
            return


def _hold(row, status, message):
    if 'validation_previous' not in row:
        row['validation_previous'] = {key: row.get(key) for key in
                                      ('status', 'needs_review', 'resume_action', 'message')}
    row.update(status=status, needs_review=True, message=message)
    row.pop('resume_action', None)
    if status == 'Changed':
        row['resume_action'] = 'extract'


def validate_rows(rows, should_stop=lambda: False):
    """Read-only filesystem checks; never search for or move a missing document."""
    for row in rows:
        if should_stop():
            _hold(row, 'Unverified', 'File check paused. Continue batch to verify before resuming.')
            continue
        intent = row.get('pending_move')
        if intent:
            source, destination = intent['source'], intent['destination']
            source_exists, destination_exists = os.path.lexists(source), os.path.lexists(destination)
            if source_exists and destination_exists:
                _hold(row, 'Recovery needed', 'Both paths of an interrupted move exist. Use Undo last batch to resolve it.')
                continue
            candidate = destination if destination_exists else source
            try:
                if fingerprint_file(candidate) != row['fingerprint']:
                    raise ValueError('Contents differ from the recorded move.')
            except (OSError, ValueError):
                _hold(row, 'Recovery needed', 'Cannot verify the interrupted move. Restore the recorded file or use Undo last batch.')
                continue
            row['source'] = candidate
            if destination_exists:
                row.update(status='Undone' if intent['action'] == 'undo' else 'Renamed', needs_review=False)
                row.pop('resume_action', None)
            elif intent['action'] == 'rename':
                row.update(status='Stopped', needs_review=True, resume_action='rename')
            else:
                row.update(status=intent.get('previous_status', 'Renamed'),
                           needs_review=intent.get('previous_needs_review', False))
                row.pop('resume_action', None)
            row.pop('pending_move', None)
            row.pop('validation_previous', None)
            row.pop('message', None)
        try:
            current = fingerprint_file(row['source'])
        except (OSError, ValueError) as exc:
            _hold(row, 'Missing', f'File missing, moved, or unreadable. Restore it to this path and Continue batch. {exc}')
            continue
        if row.get('fingerprint') and row['fingerprint'] != current:
            _hold(row, 'Changed', 'File contents changed. Continue batch will extract fresh metadata before renaming.')
            continue
        previous = row.pop('validation_previous', None)
        if previous:
            for key, value in previous.items():
                if value is None:
                    row.pop(key, None)
                else:
                    row[key] = value
        if row.get('status') in ('Ready', 'Extracted'):
            row.update(status='Stopped', needs_review=True, resume_action='rename',
                       message='Saved metadata is ready. Continue batch to finish renaming locally.')


class BatchStore:
    """One recoverable batch; credentials are never part of its saved context."""
    def __init__(self, directory):
        self.directory = Path(directory).absolute()
        self.path = self.directory / 'latest.json'
        self.data = None

    def _cache_dir(self, batch_id):
        if not re.fullmatch(r'[0-9a-f]{32}', batch_id):
            raise SessionError('Invalid saved batch ID.')
        path = self.directory / batch_id
        if self.directory.is_symlink() or path.is_symlink():
            raise SessionError('The saved batch folder must not be a symbolic link.')
        if path.resolve().parent != self.directory.resolve():
            raise SessionError('The saved batch path is outside its storage folder.')
        return path

    def start(self, paths, context):
        force_refresh = bool(context.get('force_refresh', False))
        context = {key: copy.deepcopy(context[key]) for key in ('pages', 'is_book', 'naming')}
        context['naming'] = {key: context['naming'][key] for key in ('pattern', 'author_format', 'unpublished')}
        data = {'version': 1, 'batch_id': uuid.uuid4().hex, 'context': context,
                'rows': pending_rows(paths, context['is_book'])}
        if force_refresh:
            for row in data['rows']:
                row['force_refresh'] = True
        previous = self.data
        self._write(data)
        self.data = data
        if previous:
            self._remove_cache(previous['batch_id'])
        return data['rows']

    def _remove_cache(self, batch_id):
        # Only remove our generated, flat PDF cache; never recurse through paths.
        try:
            directory = self._cache_dir(batch_id)
            for path in directory.glob('*.pdf'):
                if re.fullmatch(r'[0-9a-f]{32}\.pdf', path.name):
                    path.unlink()
            if directory.exists():
                directory.rmdir()
        except OSError:
            pass

    def _write(self, data):
        try:
            cache = self._cache_dir(data['batch_id'])
            cache.mkdir(parents=True, exist_ok=True)
            if self.path.is_symlink():
                raise SessionError('The saved batch file must not be a symbolic link.')
            payload = copy.deepcopy(data)
            for row in payload['rows']:
                snapshot = row.get('snippet_path')
                if not snapshot:
                    continue
                source = Path(snapshot)
                # Existing durable snapshots remain usable even after the temp session closes.
                if source.parent.resolve() == cache.resolve():
                    continue
                if not re.fullmatch(r'[0-9a-f]{32}\.pdf', source.name):
                    raise SessionError('Invalid review snapshot filename.')
                target = cache / source.name
                if not target.exists():
                    temporary = cache / (uuid.uuid4().hex + '.tmp')
                    try:
                        with source.open('rb') as incoming, temporary.open('xb') as outgoing:
                            shutil.copyfileobj(incoming, outgoing)
                            outgoing.flush()
                            os.fsync(outgoing.fileno())
                        os.replace(temporary, target)
                        _sync_directory(str(cache))
                    finally:
                        temporary.unlink(missing_ok=True)
                row['snippet_path'] = str(target)
            _save_journal(self.path, payload)
        except Exception as exc:
            raise SessionError(f'Cannot save batch progress to {self.path}: {exc}') from exc

    def save(self):
        if self.data is not None:
            self._write(self.data)

    def load(self):
        if not self.path.exists():
            return None
        try:
            if self.path.is_symlink():
                raise ValueError('Symbolic-link batch files are not supported.')
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if data['version'] != 1:
                raise ValueError('Unsupported batch version.')
            cache = self._cache_dir(data['batch_id'])
            context = data['context']
            if (type(context['pages']) is not int or not 1 <= context['pages'] <= 50 or
                    type(context['is_book']) is not bool or
                    not all(isinstance(context['naming'][key], str) for key in
                            ('pattern', 'author_format', 'unpublished'))):
                raise ValueError('Invalid saved processing settings.')
            if not isinstance(data['rows'], list):
                raise ValueError('Invalid saved results.')
            seen = set()
            for row in data['rows']:
                if not re.fullmatch(r'[0-9a-f]{32}', row['row_id']) or row['row_id'] in seen:
                    raise ValueError('Invalid or repeated file ID.')
                seen.add(row['row_id'])
                if not all(isinstance(row[key], str) and os.path.isabs(row[key]) and
                           row[key].lower().endswith('.pdf') for key in ('source', 'original_source')):
                    raise ValueError('Invalid PDF path.')
                if not isinstance(row['metadata'], dict) or not isinstance(row['status'], str):
                    raise ValueError('Invalid saved metadata or status.')
                if (type(row.get('analysis_attempted', False)) is not bool or
                        type(row.get('needs_review', False)) is not bool or
                        type(row.get('page_count', 0)) is not int or
                        not 0 <= row.get('page_count', 0) <= 50):
                    raise ValueError('Invalid file progress fields.')
                if 'requested_pages' in row and (type(row['requested_pages']) is not int or
                                                 not 1 <= row['requested_pages'] <= 50):
                    raise ValueError('Invalid per-file page count.')
                if 'force_refresh' in row and type(row['force_refresh']) is not bool:
                    raise ValueError('Invalid saved cache policy.')
                previous = row.get('validation_previous')
                if previous is not None and (not isinstance(previous, dict) or
                                             not isinstance(previous.get('status'), str) or
                                             type(previous.get('needs_review')) is not bool or
                                             previous.get('resume_action') not in (None, 'extract', 'rename') or
                                             not isinstance(previous.get('message', ''), (str, type(None)))):
                    raise ValueError('Invalid saved validation state.')
                meta = row['metadata']
                if (not isinstance(meta.get('authors', []), list) or
                        not all(isinstance(name, str) for name in meta.get('authors', [])) or
                        not all(isinstance(meta.get(key, ''), str) for key in ('title', 'year', 'journal'))):
                    raise ValueError('Invalid metadata fields.')
                if row.get('fingerprint') and not re.fullmatch(r'[0-9a-f]{64}', row['fingerprint']):
                    raise ValueError('Invalid file fingerprint.')
                snapshot = row.get('snippet_path')
                if snapshot and (Path(snapshot).parent.resolve() != cache.resolve() or
                                 not re.fullmatch(r'[0-9a-f]{32}\.pdf', Path(snapshot).name)):
                    raise ValueError('Review snapshot is outside the batch cache.')
                intent = row.get('pending_move')
                if intent:
                    _validate_pair(intent['source'], intent['destination'])
                    if (intent['source'] != row['source'] or intent['action'] not in ('rename', 'undo') or
                            not row.get('fingerprint')):
                        raise ValueError('Invalid interrupted move record.')
                if row.get('resume_action') not in (None, 'extract', 'rename'):
                    raise ValueError('Invalid continuation state.')
            self.data = data
            return data
        except Exception as exc:
            raise SessionError(f'Cannot restore saved batch {self.path}. The file was kept for recovery: {exc}') from exc
