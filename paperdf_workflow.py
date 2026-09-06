"""Reviewable PDF rename plans and durable, conservative batch undo.

This module has no GUI or model dependencies. Planning only reads files. A plan
is a snapshot: applying it never substitutes a different destination filename.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid


class WorkflowError(RuntimeError):
    """A journal or workflow error that needs the caller's attention."""


@dataclass
class RenamePlan:
    source: str
    proposed_name: str
    destination: str
    fingerprint: str
    status: str
    message: str = ""


@dataclass
class OperationResult:
    source: str
    destination: str
    status: str
    message: str = ""


@dataclass
class BatchResult:
    batch_id: str | None
    results: list[OperationResult]


def _absolute(path):
    return os.path.abspath(os.fspath(path))


def _key(path):
    return os.path.normcase(_absolute(path))


def _validate_name(name):
    if not isinstance(name, str) or not name or name in (".", ".."):
        raise ValueError("Enter a PDF filename.")
    if any(ord(c) < 32 or c in '<>:"/\\|?*' for c in name):
        raise ValueError("The filename contains a forbidden character or path separator.")
    if name.endswith((" ", ".")) or not name.lower().endswith(".pdf"):
        raise ValueError("The filename must end in .pdf, without trailing spaces or dots.")
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])", name.split(".")[0].rstrip(" ")):
        raise ValueError("This filename is reserved by Windows.")
    if len(name.encode("utf-8")) > 255 or len(name.encode("utf-16-le")) // 2 > 255:
        raise ValueError("The filename is too long (maximum 255 bytes / UTF-16 units).")


def _validate_pair(source, destination):
    if not os.path.isabs(source) or not os.path.isabs(destination):
        raise ValueError("File paths must be absolute.")
    if _key(Path(source).parent) != _key(Path(destination).parent):
        raise ValueError("The destination must be in the source folder.")
    if not source.lower().endswith(".pdf"):
        raise ValueError("The source must be a PDF file.")
    _validate_name(Path(destination).name)
    # Leave room for the NUL terminator in Windows extended-length paths.
    if os.name == "nt" and len(destination.encode("utf-16-le")) // 2 >= 32767:
        raise ValueError("The destination path is too long.")


def fingerprint_file(path):
    """Hash a regular, non-symlink file, rejecting detectable concurrent edits."""
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise ValueError("Only regular files are supported; symbolic links are not allowed.")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("The file changed while it was being opened.")
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
        after = os.fstat(stream.fileno())
    current = os.lstat(path)
    signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
    # Python/Windows stat and fstat can expose different ctime semantics; compare
    # ctime only between calls to the same API, never across the two APIs.
    if (signature(opened) != signature(after) or signature(after) != signature(current)
            or before.st_ctime_ns != current.st_ctime_ns or opened.st_ctime_ns != after.st_ctime_ns):
        raise ValueError("The file changed while it was being read.")
    return digest.hexdigest()


def _with_suffix(name, suffix):
    stem = name[:-4]
    ending = "_" + suffix + name[-4:]
    while stem:
        candidate = stem.rstrip(" .") + ending
        try:
            _validate_name(candidate)
            return candidate
        except ValueError:
            stem = stem[:-1]
    raise ValueError("The collision suffix does not fit in the filename.")


def plan_renames(requests, expected_fingerprints=None, should_stop=None):
    """Resolve edits and collisions without changing any files.

    Identical-content collisions are skipped, never deleted. A different-content
    collision gets a deterministic short-hash suffix, visible in the plan.
    """
    expected = {_key(p): h for p, h in (expected_fingerprints or {}).items()}
    reservations = {}
    seen_sources = set()
    plans = []
    cancelled = False
    for source, proposed in requests:
        source = _absolute(source)
        cancelled = cancelled or bool(should_stop and should_stop())
        if cancelled:
            plans.append(RenamePlan(source, str(proposed), "", "", "cancelled", "Cancelled before checking this filename. Recheck names to continue."))
            continue
        destination, fingerprint = "", ""
        try:
            _validate_name(proposed)
            destination = str(Path(source).parent / proposed)
            _validate_pair(source, destination)
            if _key(source) in seen_sources:
                raise ValueError("The same source file appears more than once.")
            seen_sources.add(_key(source))
            fingerprint = fingerprint_file(source)
            if _key(source) in expected and fingerprint != expected[_key(source)]:
                raise ValueError("The source changed after analysis; analyze it again.")
            if _key(source) == _key(destination):
                plans.append(RenamePlan(source, proposed, destination, fingerprint, "same", "The filename is already unchanged."))
                continue
            candidate, suffix_index = destination, 0
            while True:
                reserved = reservations.get(_key(candidate))
                if reserved is not None:
                    if reserved == fingerprint:
                        state, message = "duplicate", "An identical file is already planned for this name."
                        break
                elif os.path.lexists(candidate):
                    if os.path.islink(candidate):
                        raise ValueError("The destination is a symbolic link.")
                    if os.path.isfile(candidate) and fingerprint_file(candidate) == fingerprint:
                        state, message = "duplicate", "An identical file already has this name."
                        break
                else:
                    state = "ready"
                    message = "" if candidate == destination else "A hash suffix avoids an existing filename."
                    reservations[_key(candidate)] = fingerprint
                    break
                suffix_index += 1
                suffix = fingerprint[:8] if suffix_index == 1 else f"{fingerprint[:8]}_{suffix_index}"
                candidate = str(Path(source).parent / _with_suffix(proposed, suffix))
                _validate_pair(source, candidate)
            plans.append(RenamePlan(source, proposed, candidate, fingerprint, state, message))
        except (OSError, ValueError, TypeError) as exc:
            plans.append(RenamePlan(source, str(proposed), destination, fingerprint, "error", str(exc)))
    return plans


def _sync_directory(directory):
    # Windows cannot fsync directories through os.open. The journal file itself
    # is always flushed and fsynced before its atomic replacement.
    if os.name != "nt":
        fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _save_journal(path, journal):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(journal, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(str(path.parent))
    except Exception as exc:
        raise WorkflowError(f"Cannot save the batch journal {path}: {exc}") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _journal_directory(directory):
    path = Path(_absolute(directory))
    if path.is_symlink():
        raise WorkflowError("The journal folder must not be a symbolic link.")
    return path


def _read_journal(path):
    try:
        if path.is_symlink():
            raise ValueError("Symbolic-link journals are not supported.")
        journal = json.loads(path.read_text(encoding="utf-8"))
        if journal["version"] != 1 or journal["batch_id"] != path.stem:
            raise ValueError("Invalid journal version or batch ID.")
        if not isinstance(journal["operations"], list):
            raise ValueError("Invalid operation list.")
        seen = set()
        for op in journal["operations"]:
            _validate_pair(op["source"], op["destination"])
            if _key(op["source"]) == _key(op["destination"]) or _key(op["source"]) in seen:
                raise ValueError("Invalid or duplicate source path.")
            seen.add(_key(op["source"]))
            if not re.fullmatch(r"[0-9a-f]{64}", op["fingerprint"]):
                raise ValueError("Invalid file fingerprint.")
            if op["state"] not in {"planned", "pending", "renamed", "failed", "undo_pending", "restored"}:
                raise ValueError("Invalid operation state.")
        return journal
    except Exception as exc:
        raise WorkflowError(f"Cannot read the batch journal {path}; undo stopped: {exc}") from exc


def _move_no_clobber(source, destination):
    """Move without ever replacing an existing directory entry."""
    if os.name == "nt":
        # Unlike POSIX rename, Windows rename fails if the target exists.
        os.rename(source, destination)
    else:
        # Both paths are siblings. Hard-link creation is atomic and fails if the
        # target exists; the journal also recovers a crash between link/unlink.
        os.link(source, destination, follow_symlinks=False)
        try:
            if os.path.islink(destination) or not os.path.samefile(source, destination):
                raise OSError("The source changed during the move.")
            os.unlink(source)
        except Exception:
            # Preserve both entries for recovery if unlink did not finish.
            raise
    _sync_directory(str(Path(destination).parent))


def _require_content(path, expected):
    if fingerprint_file(path) != expected:
        raise ValueError("File contents changed; operation refused.")


def _emit(results, result, callback):
    results.append(result)
    if callback is not None:
        callback(result)


def apply_batch(plans, journal_dir, should_stop=None, on_result=None, before_move=None):
    """Apply exactly the supplied reviewed plans; never overwrite or replan."""
    plans = list(plans)
    results = []
    ready = [p for p in plans if p.status == "ready"]
    path = journal = None
    batch_id = None
    if ready and not (should_stop and should_stop()):
        seen = set()
        for plan in ready:
            _validate_pair(plan.source, plan.destination)
            if _key(plan.source) in seen or _key(plan.source) == _key(plan.destination):
                raise WorkflowError("The batch contains an invalid or repeated source.")
            if not re.fullmatch(r"[0-9a-f]{64}", plan.fingerprint):
                raise WorkflowError("The plan has no valid source fingerprint.")
            seen.add(_key(plan.source))
        directory = _journal_directory(journal_dir)
        directory.mkdir(parents=True, exist_ok=True)
        batch_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "_" + uuid.uuid4().hex
        path = directory / (batch_id + ".json")
        journal = {"version": 1, "batch_id": batch_id, "operations": [
            {"source": p.source, "destination": p.destination, "fingerprint": p.fingerprint, "state": "planned", "message": ""}
            for p in ready
        ]}
        _save_journal(path, journal)
    ready_index = 0
    for plan in plans:
        if plan.status != "ready":
            _emit(results, OperationResult(plan.source, plan.destination, "failed" if plan.status == "error" else "skipped", plan.message), on_result)
            continue
        op = journal["operations"][ready_index] if journal is not None else None
        ready_index += 1
        if op is None or (should_stop and should_stop()):
            _emit(results, OperationResult(plan.source, plan.destination, "skipped", "Cancelled before renaming."), on_result)
            continue
        try:
            _require_content(plan.source, plan.fingerprint)
            if os.path.lexists(plan.destination):
                raise FileExistsError("The reviewed destination is now occupied; preview again.")
        except (OSError, ValueError) as exc:
            op.update(state="failed", message=str(exc))
            _save_journal(path, journal)
            _emit(results, OperationResult(plan.source, plan.destination, "failed", str(exc)), on_result)
            continue
        op["state"] = "pending"
        _save_journal(path, journal)
        if before_move:
            before_move(OperationResult(plan.source, plan.destination, 'pending'))
        try:
            _require_content(plan.source, plan.fingerprint)
            _move_no_clobber(plan.source, plan.destination)
        except (OSError, ValueError) as exc:
            # An error may follow a successful move (directory fsync) or partial
            # POSIX link/unlink. Keep pending so undo inspects the actual files.
            op["message"] = str(exc)
            _save_journal(path, journal)
            _emit(results, OperationResult(plan.source, plan.destination, "failed", str(exc)), on_result)
            continue
        op.update(state="renamed", message="")
        _save_journal(path, journal)
        _emit(results, OperationResult(plan.source, plan.destination, "renamed"), on_result)
    return BatchResult(batch_id, results)


def _undo_operation(op):
    original, renamed, fingerprint = op["source"], op["destination"], op["fingerprint"]
    original_exists, renamed_exists = os.path.lexists(original), os.path.lexists(renamed)
    if original_exists and renamed_exists:
        _require_content(original, fingerprint)
        _require_content(renamed, fingerprint)
        if op["state"] in {"pending", "undo_pending"} and os.path.samefile(original, renamed):
            # Recovery of our own interrupted POSIX hard-link move. Removing the
            # renamed alias leaves the original entry and all file data intact.
            os.unlink(renamed)
            _sync_directory(str(Path(original).parent))
            return "restored", "Recovered an interrupted move."
        raise FileExistsError("The original filename is occupied; undo refused.")
    if original_exists:
        _require_content(original, fingerprint)
        if op["state"] == "undo_pending":
            return "restored", "Recovered an already completed undo."
        if op["state"] == "pending":
            return "skipped", "The interrupted rename did not change the filename."
        raise FileNotFoundError("The renamed file is missing; undo cannot verify its location.")
    if not renamed_exists:
        raise FileNotFoundError("Neither the original nor the renamed file exists.")
    _require_content(renamed, fingerprint)
    return "move", ""


def undo_last_batch(journal_dir, should_stop=None, on_result=None, before_move=None):
    """Undo the newest unfinished batch, retaining failures for a later retry."""
    directory = _journal_directory(journal_dir)
    if not directory.exists():
        return BatchResult(None, [])
    active = {"pending", "renamed", "undo_pending"}
    for path in sorted(directory.glob("*.json"), reverse=True):
        journal = _read_journal(path)  # Corruption must not silently select older work.
        if any(op["state"] in active for op in journal["operations"]):
            break
    else:
        return BatchResult(None, [])
    results = []
    for op in reversed(journal["operations"]):
        if op["state"] not in active:
            continue
        if should_stop and should_stop():
            _emit(results, OperationResult(op["destination"], op["source"], "skipped", "Cancelled before undo."), on_result)
            continue
        if before_move:
            before_move(OperationResult(op['destination'], op['source'], 'pending'))
        try:
            action, message = _undo_operation(op)
        except (OSError, ValueError) as exc:
            op["message"] = str(exc)
            _save_journal(path, journal)
            _emit(results, OperationResult(op["destination"], op["source"], "failed", str(exc)), on_result)
            continue
        if action == "move":
            op["state"] = "undo_pending"
            _save_journal(path, journal)
            try:
                # Recheck after persistence, immediately before the move.
                _require_content(op["destination"], op["fingerprint"])
                _move_no_clobber(op["destination"], op["source"])
                action, message = "restored", ""
            except (OSError, ValueError) as exc:
                op["message"] = str(exc)
                _save_journal(path, journal)
                _emit(results, OperationResult(op["destination"], op["source"], "failed", str(exc)), on_result)
                continue
        op.update(state="restored" if action == "restored" else "failed", message=message)
        _save_journal(path, journal)
        _emit(results, OperationResult(op["destination"], op["source"], action, message), on_result)
    return BatchResult(journal["batch_id"], results)
