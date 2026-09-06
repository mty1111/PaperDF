# Command-line workflow (1.4.0)

The CLI uses the same extraction, strict response schema, academic rules, cache and safe rename/undo services as the desktop app. It imports no Tk UI and can run without a display.

Install the repository dependencies, then run from the source checkout:

```powershell
python -m paperdf_cli --help
python -m paperdf_cli run "D:\Papers\paper.pdf"
python -m paperdf_cli continue --apply
python -m paperdf_cli status --csv "D:\Papers\results.csv"
python -m paperdf_cli undo
```

Windows releases also include `PaperDF-cli-v1.4.0-windows.exe` with a checksum. Replace `python -m paperdf_cli` with the console executable path. The ordinary `PaperDF-v1.4.0-windows.exe` remains the desktop application. macOS/Linux CLI users currently run from Python source; their standalone packages still contain the desktop application.

## Commands

| Command | Behavior |
| --- | --- |
| `run FILE_OR_FOLDER ...` | Analyze PDFs and preview automatic-policy filenames; save the CLI batch without renaming. |
| `run ... --apply` | Analyze and rename complete, unambiguous results; hold incomplete metadata and conflicts. |
| `continue` | Validate current file contents, retry pending/failed extraction and preview remaining names using saved settings. |
| `continue --apply` | Finish the saved batch. Unchanged, already extracted files need no new model request. |
| `status` | Read saved CLI results only; no requests, file validation, or renames. |
| `undo` | Immediately undo the newest unfinished batch in the shared rename history, which may have been created by the GUI. |

Preview avoids renaming PDFs but can make paid Gemini requests, write extraction cache entries and save batch state. `continue` can also make requests for pending, failed or changed files. A saved plan does not bypass content or collision checks when applying it.

Folder input reads PDFs directly within the folder. Add `--recursive` to include subfolders. Repeated paths are deduplicated, symbolic-link inputs are rejected, and the selected storage directory's generated GUI/CLI batch snapshots are excluded from folder scanning.

## Settings and output

Global options go **before** the command:

```powershell
python -m paperdf_cli --store-dir "D:\PaperDF-state" --quiet run "D:\Papers" --recursive --pages 6 --apply
```

`--store-dir` chooses configuration, shared extraction cache and undo-history storage. Without it, the CLI uses the desktop storage directory (`%LOCALAPPDATA%\pdfrenamer` on Windows, otherwise `~/pdfrenamer`). Settings are read explicitly and are never rewritten by the CLI. Credentials use the same precedence as the GUI: saved `api_key`, then `GEMINI_API_KEY`, then the local dotenv file. API keys are not command-line arguments or saved batch fields. All-cache-hit batches work without credentials.

New runs accept `--book`, `--pages 1..50`, `--model`, `--pattern`, `--author-format`, `--title-style preserve|title`, `--aliases-file FILE` and `--force-refresh`. Alias files use UTF-8 and the existing `alias = journal` syntax; an empty file disables aliases. Defaults are four paper pages or twenty book pages. Naming settings, page counts and mode are frozen in the saved batch. Continuation reads current credentials and model; `continue --model NAME` can override the model for unfinished extractions. Completed metadata keeps its saved naming settings.

CLI results live in `cli-batch-state`; desktop results remain in `batch-state`. Both use `extraction-cache.sqlite3` and `rename-history`. Starting a new CLI batch does not erase the desktop batch. If CLI results still have pending actions, use `continue` or explicitly replace them with `run --new-batch`. Replacement preserves undo history and the cross-batch cache. Only the newest CLI batch is retained.

Standard output is one UTF-8 JSON object containing `command`, `summary` and `rows`; run/continue also report `applied` and `stopped`, and undo includes operations and batch ID. Summary includes total, analyzed, renamed, needs_attention and per-status counts. `renamed` counts distinct files whose tracked current path differs from the original, so undo reduces it. Progress such as `Analyzed: 3 / 10` and `Renamed: 2 / 10` goes to standard error; `--quiet` suppresses progress. `status` shows the saved snapshot, not a fresh filesystem audit.

`run`, `continue` and `status` accept `--csv FILE.csv` to export the complete result set. Errors go to standard error. A CSV failure may occur after renaming; the saved batch and undo journal still describe the applied changes.

| Exit code | Meaning |
| --- | --- |
| 0 | Command completed; processing has no rows needing attention. A preview's Ready rows are successful plans. |
| 1 | Processing left rows needing attention, or undo had failed operations. Inspect the JSON results. |
| 2 | Invalid arguments/settings, unavailable saved state, or a command-level failure. |
| 130 | Interrupted. The current extraction may finish before stopping; completed work is checkpointed. |

Ctrl+C requests cancellation between files and before subsequent renames. Use `continue` after restarting. Missing or ambiguous source paths remain held for recovery, and changed PDFs require fresh extraction. Name conflicts are not automatically renamed using generated suffixes.

## Current boundaries

CLI and GUI share services but have separate latest-batch views. The CLI currently has no interactive metadata editor; held results are available in JSON/CSV, and users can process/review the file through the GUI using the shared extraction cache. Local corrections are specific to their saved batch.

Avoid simultaneous GUI/CLI operations on the same PDFs or simultaneous CLI commands using the same state directory. There is no cross-process batch lock. Shared undo always selects the latest unfinished journal, so use a separate `--store-dir` if you need independent undo histories.

This is the first stage 4 delivery. Gemini remains the only provider; additional providers and local metadata extraction are not yet implemented. Real Gemini/PDF acceptance remains deferred; automated CLI tests use synthetic PDFs and mocked provider responses.
