"""Persistent extraction results and attempt records across independent batches."""
import hashlib
import json
from pathlib import Path
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from paperdf_schema import FIELDS, validate_metadata

# Bump whenever prompt, response contract, or normalization rules change.
EXTRACTION_RULES_VERSION = 'gemini-frontmatter-strict-v1'


class ExtractionCache:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        try:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1):
                raise ValueError('Unsupported extraction-cache version.')
            db.execute('CREATE TABLE IF NOT EXISTS results (key TEXT PRIMARY KEY, context TEXT NOT NULL, '
                       'metadata TEXT NOT NULL, snippet BLOB NOT NULL, snippet_hash TEXT NOT NULL, '
                       'page_count INTEGER NOT NULL, updated_at TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS attempts (id INTEGER PRIMARY KEY, key TEXT NOT NULL, '
                       'source TEXT NOT NULL, outcome TEXT NOT NULL, error_type TEXT NOT NULL, created_at TEXT NOT NULL)')
            db.execute('PRAGMA user_version=1')
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def context(fingerprint, pages, is_book, model, rules=EXTRACTION_RULES_VERSION):
        return {'sha256': fingerprint, 'pages': pages, 'is_book': bool(is_book),
                'provider': 'gemini', 'model': model, 'rules': rules}

    @staticmethod
    def key(context):
        return hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()

    def get(self, context):
        with self.connection() as db:
            record = db.execute('SELECT metadata, snippet, snippet_hash, page_count FROM results WHERE key=?',
                                (self.key(context),)).fetchone()
        if record is None:
            return None
        metadata, snippet, digest, pages = record
        if hashlib.sha256(snippet).hexdigest() != digest:
            raise ValueError('Extraction cache is damaged. Clear extraction cache in Config before retrying.')
        if type(pages) is not int or not 1 <= pages <= context['pages']:
            raise ValueError('Invalid cached page count.')
        metadata = validate_metadata(json.loads(metadata), pages, normalized=True)
        return metadata, snippet, pages

    def put(self, context, metadata, snippet, page_count):
        # Persist canonical model metadata only, never manual filename overrides.
        metadata = {field: metadata.get(field, [] if field == 'authors' else '') for field in FIELDS} | {
            'evidence': {field: metadata.get('evidence', {}).get(field, []) for field in FIELDS}}
        validate_metadata(metadata, page_count, normalized=True)
        with self.connection() as db:
            db.execute('INSERT OR REPLACE INTO results VALUES (?, ?, ?, ?, ?, ?, ?)',
                       (self.key(context), json.dumps(context, sort_keys=True), json.dumps(metadata, ensure_ascii=False),
                        snippet, hashlib.sha256(snippet).hexdigest(), page_count, self.now()))

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat()

    def record(self, context, source, outcome, error_type=''):
        # No API keys or raw provider error messages are stored in this history.
        with self.connection() as db:
            db.execute('INSERT INTO attempts(key, source, outcome, error_type, created_at) VALUES (?, ?, ?, ?, ?)',
                       (self.key(context), source, outcome, error_type, self.now()))

    def clear(self):
        with self.connection() as db:
            db.execute('DELETE FROM results')
            db.execute('DELETE FROM attempts')
        # Release pages containing old metadata and PDF prefixes.
        with self.connection() as db:
            db.execute('VACUUM')
