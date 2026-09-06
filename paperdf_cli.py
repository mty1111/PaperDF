"""Headless CLI. Run `python -m paperdf_cli --help` for commands."""
import argparse
import configparser
from collections import Counter
from dataclasses import asdict
import json
import logging
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading

from paperdf_config import default_store_dir, load_config, api_key_for
from paperdf_defaults import (DEFAULT_MODEL, DEFAULT_PAPER_PAGES, DEFAULT_BOOK_PAGES, MAX_PAGES_TO_EXTRACT,
                              DEFAULT_OUTPUT_PATTERN, DEFAULT_BOOK_OUTPUT_PATTERN, DEFAULT_AUTHOR_FMT_PAPER,
                              DEFAULT_AUTHOR_FMT_BOOK, DEFAULT_UNPUBLISHED)
from paperdf_academic import DEFAULT_ALIASES, parse_aliases
from paperdf_cache import ExtractionCache
from paperdf_core import extract_rows
from paperdf_gemini import _LazyGeminiClient
from paperdf_naming import build_new_filename
from paperdf_processing import plan_rows, process_rows, record_results
from paperdf_session import BatchStore, can_continue, remember_move, validate_rows
from paperdf_workflow import undo_last_batch
from paperdf_export import export_results


def version():
    return (Path(__file__).parent / 'VERSION.txt').read_text(encoding='utf-8').strip()


def page_count(value):
    try:
        count = int(value)
        if 1 <= count <= MAX_PAGES_TO_EXTRACT:
            return count
    except ValueError:
        pass
    raise argparse.ArgumentTypeError(f'pages must be between 1 and {MAX_PAGES_TO_EXTRACT}')


def parser():
    result = argparse.ArgumentParser(description='PaperDF headless PDF renaming; run previews by default.')
    result.add_argument('--version', action='version', version=version())
    result.add_argument('--store-dir', type=Path, default=default_store_dir(),
                        help='configuration, shared extraction cache and undo history directory')
    result.add_argument('--quiet', action='store_true', help='suppress progress on stderr')
    commands = result.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run', help='analyze PDFs and preview filenames; add --apply to rename')
    run.add_argument('paths', nargs='+', type=Path)
    run.add_argument('--recursive', action='store_true', help='include PDF subfolders')
    run.add_argument('--book', action='store_true')
    run.add_argument('--pages', type=page_count)
    run.add_argument('--model', help='override configured Gemini model')
    run.add_argument('--pattern', help='override filename pattern')
    run.add_argument('--author-format')
    run.add_argument('--title-style', choices=('preserve', 'title'))
    run.add_argument('--aliases-file', type=Path, help='UTF-8 alias = journal mappings; an empty file disables aliases')
    run.add_argument('--force-refresh', action='store_true')
    run.add_argument('--new-batch', action='store_true', help='replace unfinished CLI results (keeps undo history)')
    run.add_argument('--apply', action='store_true')
    resume = commands.add_parser('continue', help='resume saved CLI results; add --apply to rename')
    resume.add_argument('--apply', action='store_true')
    resume.add_argument('--model', help='Gemini model for unfinished extractions; completed metadata stays local')
    commands.add_parser('status', help='show saved CLI results without requests or renames')
    commands.add_parser('undo', help='undo the newest unfinished rename batch in the shared history')
    for name in ('run', 'continue', 'status'):
        commands.choices[name].add_argument('--csv', type=Path, help='export full results to a CSV report')
    return result


def collect_paths(paths, recursive, directory):
    found = {}
    excluded = {(directory / name).resolve() for name in ('batch-state', 'cli-batch-state')}
    for path in paths:
        if path.is_symlink():
            raise ValueError(f'Symbolic-link inputs are not supported: {path}')
        if path.is_dir():
            for root, folders, files in os.walk(path):
                folders[:] = sorted(name for name in folders if (Path(root) / name).resolve() not in excluded
                                     and not (Path(root) / name).is_symlink()) if recursive else []
                if Path(root).resolve() in excluded:
                    folders[:] = []
                    continue
                for name in sorted(files):
                    if name.lower().endswith('.pdf'):
                        item = os.path.abspath(Path(root) / name)
                        found.setdefault(os.path.normcase(item), item)
        elif path.suffix.lower() == '.pdf':
            item = os.path.abspath(path)
            found.setdefault(os.path.normcase(item), item)
        else:
            raise ValueError(f'Expected a PDF file or folder: {path}')
    if not found:
        raise ValueError('No PDF files found.')
    return list(found.values())


def naming_settings(args, settings):
    book = args.book
    naming = {
        'pattern': args.pattern if args.pattern is not None else settings.get(
            'book_output_pattern' if book else 'output_pattern', DEFAULT_BOOK_OUTPUT_PATTERN if book else DEFAULT_OUTPUT_PATTERN),
        'author_format': args.author_format if args.author_format is not None else settings.get(
            'author_format_book' if book else 'author_format_paper', DEFAULT_AUTHOR_FMT_BOOK if book else DEFAULT_AUTHOR_FMT_PAPER),
        'unpublished': settings.get('unpublished', DEFAULT_UNPUBLISHED),
        'title_style': args.title_style or settings.get('title_style', 'preserve'),
        'journal_aliases': args.aliases_file.read_text(encoding='utf-8-sig') if args.aliases_file else settings.get('journal_aliases', DEFAULT_ALIASES),
    }
    parse_aliases(naming['journal_aliases'])
    build_new_filename({'authors': ['Author'], 'title': 'Title', 'year': '2000'}, book, naming)
    return naming


def snapshot(rows):
    return {'total': len(rows), 'analyzed': sum(bool(row.get('analysis_attempted')) for row in rows),
            'renamed': sum(os.path.normcase(row['source']) != os.path.normcase(row.get('original_source', row['source']))
                           for row in rows),
            'needs_attention': sum(bool(row.get('needs_review')) for row in rows),
            'statuses': dict(Counter(row['status'] for row in rows))}


def execute(args, *, stopped=None, client_factory=_LazyGeminiClient, out=None, err=None):
    """Run one command. Dependencies are injectable for offline integration tests."""
    out, err = out or sys.stdout, err or sys.stderr
    stopped = stopped or threading.Event()
    directory = args.store_dir.absolute()
    store = BatchStore(directory / 'cli-batch-state')
    data = store.load()
    rows = data['rows'] if data else []

    def emit(**extra):
        payload = dict(command=args.command, summary=snapshot(rows), rows=rows, **extra)
        if getattr(args, 'csv', None):
            export_results(rows, args.csv)
        print(json.dumps(payload, ensure_ascii=False), file=out)

    if args.command == 'status':
        emit()
        return 0

    def before_move(result, action='rename'):
        remember_move(rows, result, action)
        store.save()

    def after_move(result):
        record_results(rows, [result])
        store.save()
        if not args.quiet:
            counts = snapshot(rows)
            print(f"Renamed: {counts['renamed']} / {counts['total']}", file=err)

    if args.command == 'undo':
        result = undo_last_batch(directory / 'rename-history', stopped.is_set, after_move,
                                 lambda op: before_move(op, 'undo'))
        emit(operations=[asdict(op) for op in result.results], batch_id=result.batch_id)
        return 130 if stopped.is_set() else int(any(op.status == 'failed' for op in result.results))

    settings = load_config(directory)['Settings']
    model = args.model or settings.get('model', DEFAULT_MODEL)
    if args.command == 'run':
        if any(can_continue(row) for row in rows) and not args.new_batch:
            raise ValueError('Unfinished CLI results exist. Use continue or run --new-batch.')
        paths = collect_paths(args.paths, args.recursive, directory)
        context = {'is_book': args.book, 'pages': args.pages or (DEFAULT_BOOK_PAGES if args.book else DEFAULT_PAPER_PAGES),
                   'naming': naming_settings(args, settings), 'force_refresh': args.force_refresh}
        rows = store.start(paths, context)
    else:
        if not data:
            raise ValueError('No saved CLI batch. Start with run.')
        context = data['context']
        validate_rows(rows, stopped.is_set)
        store.save()
    build_name = lambda meta: build_new_filename(meta, context['is_book'], context['naming'])
    candidates = [row for row in rows if can_continue(row)]
    ready = [row for row in candidates if row.get('resume_action') == 'rename' and not row.get('pending_move')]
    to_extract = [row for row in candidates if row.get('resume_action') == 'extract']
    sdk = client_factory(api_key_for(directory, settings))
    retried = []

    def progress(index, total, row):
        store.save()
        if not args.quiet:
            counts = snapshot(rows)
            print(f"Analyzed: {counts['analyzed']} / {counts['total']} — {row['status']}", file=err)

    try:
        with tempfile.TemporaryDirectory(prefix='paperdf-cli-') as review:
            try:
                retried = extract_rows(to_extract, context['pages'], context['is_book'], sdk, model,
                                       stopped.is_set, progress, review, ExtractionCache(directory / 'extraction-cache.sqlite3'))
            finally:
                store.save()  # persist exact PDF prefixes before temporary cleanup
        if not stopped.is_set():
            for row in ready:
                row.update(needs_review=False, selected=True)
            if args.apply:
                process_rows(retried + ready, build_name, directory / 'rename-history', stopped.is_set,
                             after_move, before_move)
            else:
                _, duplicates = plan_rows(retried + ready, build_name, stopped.is_set)
                # A dry-run duplicate may refer to a target that is only planned.
                # Recheck it at apply time even if another source changes meanwhile.
                for row, _ in duplicates:
                    row['resume_action'] = 'rename'
        store.save()
    finally:
        try:
            sdk.close()
        except Exception:
            logging.warning('Could not close Gemini client', exc_info=True)
    emit(applied=args.apply, stopped=stopped.is_set())
    return 130 if stopped.is_set() else int(any(row.get('needs_review') for row in rows))


def main(argv=None):
    # Keep redirected JSON and progress usable for Unicode PDF paths on Windows.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    args = parser().parse_args(argv)
    stopped = threading.Event()
    previous = signal.signal(signal.SIGINT, lambda *_: stopped.set())
    try:
        return execute(args, stopped=stopped)
    except (OSError, ValueError, RuntimeError, KeyError, configparser.Error) as exc:
        print(f'PaperDF: {exc}', file=sys.stderr)
        return 2
    finally:
        signal.signal(signal.SIGINT, previous)


if __name__ == '__main__':
    sys.exit(main())
