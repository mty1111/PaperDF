# PaperDF roadmap status

## Current delivery: 1.4.0 pre-release

Stages 2 and 3 implementation are complete; stage 4 has its first delivery (headless core and CLI); real Gemini/PDF acceptance remains deferred by user request. CI and release outcomes are recorded on the matching GitHub commit/tag; do not infer installation or real-provider validation from a successful package build.

| Stage | Status | Scope |
| --- | --- | --- |
| 1: inspect and recover | Implemented with revised interaction | Metadata result table, multipage PDF review, local corrections and durable undo. Automatic normal renames replaced the originally proposed mandatory checkbox approval flow. |
| 2: avoid redundant requests and incorrect skips | Implemented; offline regression verified locally | See completion checklist below. |
| 3: academic metadata details | Implemented; offline regression verified locally | Structured compound surnames and institutional authors, configurable title styles and journal aliases, and explicit document-version years with review controls. |
| 4: architecture and expansion | In progress; headless core and CLI implemented | Shared extraction, naming and configuration services; CLI preview/apply/continue/status/undo and CSV; separate Windows console build. Additional providers and local extraction remain demand-driven follow-up work. |

## Stage 2 completion checklist

- Cross-batch persistent result cache plus timestamped attempt records: content SHA-256, requested page count, paper/book mode, provider/model and versioned extraction rules.
- Reuse after renames/moves/restarts, with exact analyzed PDF-prefix bytes and hashes. New naming settings do not trigger another extraction. New batches do not erase the long-term cache.
- No filename-only skip; changed contents/settings miss cache. Cache hits still pass normal completeness and safe rename policy. Manual corrections remain local to the saved batch.
- Cache-only processing does not require credentials or a provider client. Lazy connection failures leave uncached files retryable.
- Exact local response validation: required/unknown/duplicate keys, field types, year format, evidence structures, quote lengths and physical page bounds. Empty fields are held for review; malformed responses are errors, never cache successes.
- Failed-file retry and saved continuation use cache policy. Forced refresh survives pending/failed files and restart; single-file reanalysis always refreshes.
- Transactional cache writes, content checks before committing results, damaged-cache reporting, cache-source indicators, and explicit clearing of cache/attempt records.

## Stage 3 completion checklist

- Structured author identity and given/family/suffix parts preserve compound surnames, particles, accents and hyphens. Institutional and unknown names render literally; uncertain types/family names require review.
- Local Name parts editor, manual version-year selection and source-page navigation integrate with correction, saved results and durable undo.
- Preserve extracted capitalization by default; optional local title case protects known acronyms, mixed-case terms and marked math tokens.
- Configurable exact journal aliases with conflict validation, UTF-8 persistence and publisher-mode bypass. Source metadata is retained.
- Explicit year policy for publication/online dates, preprint revisions and current book editions. Ambiguous or unsupported dates are held for review.
- Extended strict schema validates aligned author details and quoted date candidates. Versioned cache rules prevent old extraction entries from standing in for new structured responses; old saved batches remain readable.
- Rules, examples, registry sources and limitations: [academic-rules.md](docs/academic-rules.md).

## Stage 4 first delivery

- Move PDF-prefix extraction, cache orchestration, retry/reanalysis and filename building out of the GUI. Shared services import no Tk and do not load settings or create storage at import time.
- Isolate Gemini request/cleanup and normalization in an adapter; centralize defaults and explicit configuration loading. Keep desktop compatibility wrappers passing current settings.
- Add CLI run (preview by default, `--apply` to rename), continue, status, CSV and undo. Save independent CLI batch state while sharing the extraction cache and undo history.
- Preserve content checks, conflict holds, schema failures, academic review policy, cancellation checkpoints and forced refresh through restart.
- Provide a Windows console executable alongside the desktop executable. The Python CLI runs on all supported systems without a display.
- [CLI usage and limits](docs/cli.md); [architecture and provider extension boundary](docs/architecture.md). Other providers and local metadata extraction remain unimplemented, as planned for demand-driven work.

## Limits and recovery

- Concurrent commands sharing a state directory, or concurrent GUI/CLI operations on the same PDFs, are not supported by a cross-process batch lock. CLI undo uses the shared latest unfinished journal, including GUI operations; a separate storage directory gives independent history.
- Model name parts and date classifications may be wrong; quotations are not independently checked against the PDF. All-caps unfamiliar acronyms and unstructured names may require manual edits. The built-in journal registry is intentionally small; the version-year policy is an app policy, not a universal citation standard.
- Provider responses lost before the cache commit can require another request. Separate simultaneously running app instances may race to request the same uncached PDF; there is no distributed request lock.
- Model identifiers are cache keys; provider-side updates under an unchanged identifier require explicit refresh. Extraction rule changes must bump `EXTRACTION_RULES_VERSION`.
- Entries persist until cleared. Config clearing preserves latest-batch state and rename journals. If the database itself cannot be opened, close the app and move `extraction-cache.sqlite3` aside to preserve it before retrying.
- The latest saved batch keeps original extraction settings and local corrections; strict response validation applies to new model responses, not retroactively to saved/manual metadata.

Local baseline verification: `python scripts/run_tests.py` — 170 tests, 169 passed, one Windows symlink-permission skip. Tests use synthetic PDFs and mocked provider responses. Publication and remote CI must be checked separately.
