# Core and entrypoints

The first stage 4 delivery moves PDF extraction and filename construction out of the desktop module. Both entrypoints use the same implementation. Importing the headless services does not create a Tk window, import the GUI, create a storage directory or load user settings.

| Module | Responsibility |
| --- | --- |
| `pdf_metadata_renamer.py`, `paperdf_preview.py`, `paperdf_review.py`, `paperdf_author_editor.py` | Tk windows, input capture, worker dispatch, rendering and interactive corrections. Thin desktop wrappers pass current settings into the services. |
| `paperdf_cli.py` | Argument parsing, terminal output and saved CLI batch orchestration. |
| `paperdf_core.py` | PDF prefixes, extraction progress/cancellation, cache orchestration, selected-row retry and reanalysis. |
| `paperdf_gemini.py` | Gemini upload/request/cleanup, strict response contract and metadata normalization. |
| `paperdf_naming.py`, `paperdf_academic.py` | Pure filename building and academic name/title/journal/year rules. |
| `paperdf_config.py`, `paperdf_defaults.py` | Explicit settings/credential loading and defaults, independent of UI. |
| `paperdf_cache.py`, `paperdf_schema.py` | Durable extraction cache/attempt records and response validation. |
| `paperdf_processing.py` | Shared automatic planning/applying policy and explicit local corrections. `plan_rows` does not move files or write rename journals. |
| `paperdf_workflow.py` | Content checks, no-overwrite file operations and durable undo. |
| `paperdf_session.py`, `paperdf_export.py` | Atomic batch checkpoints/recovery and CSV exports. |

The extraction core accepts a callable provider boundary. The default adapter is Gemini and cache keys still explicitly identify Gemini. This is a seam for future work, not an implemented multi-provider abstraction. A new provider will also need its own identity/rule version, validated response contract, credentials, error policy and tests. Local text extraction must preserve page provenance and define a conservative fallback for scanned/ambiguous PDFs before it can replace model extraction.

GUI and CLI retain entrypoint-specific orchestration and separate latest-batch manifests. Further controller consolidation is possible after both flows have settled; it is not required to run the present CLI without a display. Existing desktop wrapper functions remain for compatibility, while the shared rules have a single implementation.

Offline regressions cover the existing GUI workflows plus a subprocess that forbids Tk imports, CLI preview/apply/restart/undo, cancellation, cache-only requests, forced refresh, changed source files, ambiguous dates, conflicts, configuration, CSV and state isolation. Real-provider acceptance and concurrent multi-process operation are separate concerns.
