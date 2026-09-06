# Changelog

## 1.4.0 — 2026-09-06 (pre-release)

- Start stage 4 with shared headless PDF extraction, retry/reanalysis, naming and configuration modules. Keep the desktop workflow on the same services; isolate Gemini requests and response normalization in an adapter.
- Add CLI run (preview by default, `--apply` to rename), continue, status/CSV and undo. Preserve schema checks, academic review rules, cache reuse, changed-file checks, conflict holds and cancellation checkpoints.
- Keep CLI latest-batch state separate from GUI state while sharing extraction cache and durable undo history. Expose frozen naming settings, page/model controls, force refresh, recursive input, UTF-8 JSON results, X / total progress and documented exit codes.
- Add a Windows console executable and checksum alongside the desktop build, with packaged `--version`/`--help` smoke checks. Source CLI works without Tk or a display on supported platforms.
- Add 15 offline CLI/config integration tests; total 170 tests (169 passed locally, one Windows symlink-permission skip). Real Gemini/PDF acceptance remains deferred. Additional model providers and local metadata extraction remain future work driven by demand.

## 1.3.0 — 2026-09-06 (pre-release)

- Complete stage 3 implementation: structured compound surnames and institutional authors, conservative capitalization, configurable journal aliases and document-version year selection.
- Preserve full family names, accents, particles and suffixes. Organizations use their literal names; unknown author types/family names are held for review. Add a local Name parts editor integrated with correction and undo.
- Preserve extracted capitalization by default; optional title case protects known acronyms and mixed-case terms. Apply exact configurable journal aliases locally, excluding book publishers. Save Unicode aliases and freeze naming settings for continuation.
- Prefer publication then online year for published articles, latest reported revision for preprints, and the current edition's publication year for books. Show dated quotations, year choices and PDF page navigation. Ambiguous or unsupported dates require review.
- Extend strict validation to aligned author details and quoted date candidates; version the extraction cache rules. Preserve new academic data through cache reuse and saved batches while keeping old saved batches readable.
- Offline validation: 155 tests, 154 passed locally and one Windows symlink-permission skip. Tests cover academic policies, strict schema, cache, correction, year navigation, saved settings and undo. Real Gemini/PDF acceptance remains deferred.

## 1.2.0 — 2026-09-06 (pre-release)

- Complete the second roadmap stage's implementation: persistent cross-batch extraction cache and attempt records, strict local provider-response validation, and integration with failed-file retry and saved-batch continuation.
- Match full-content SHA-256, requested pages, paper/book mode, model and extraction-rule version. Reuse exact PDF prefixes and extracted metadata after renames, moves and restarts. Cached batches need no Gemini credentials or client connection; naming settings are applied locally.
- Add a force-refresh checkbox for new batches; preserve that choice for unfinished/failed files across restart. Single-file reanalysis always bypasses cache and updates it only after success.
- Reject non-schema, duplicate-key, incorrectly typed and invalid-citation responses as retryable errors. Empty metadata remains reviewable, including on cache hits. Existing batch files stay readable.
- Add cache clearing in Config, cache-source details/logs, and offline cache/GUI regression tests. Cache failures are surfaced and failed writes retain prior committed results. Real Gemini/PDF acceptance remains deferred.

## 1.1.2 — 2026-09-06 (pre-release)

- Add **Export all results...** to save the current batch as a CSV report, including original/current paths, metadata, statuses, page counts, and problem details. Export includes rows hidden by the attention filter and makes no model requests.
- Write UTF-8 with BOM for Unicode spreadsheet compatibility, preserve multi-line fields, and prefix formula-like cells as text. Failed writes preserve an existing report.
- Add offline coverage for CSV contents, export cancellation, busy-state guards, filtered results, and failed saves. Real Gemini/PDF acceptance remains deferred.

## 1.1.1 — 2026-09-06 (pre-release)

- Add **Reanalyze file...** to request a different first-page count for one result, including an already renamed file. Successful extraction replaces its metadata and analyzed-page snapshot for review; filename changes require an explicit correction.
- Keep the previous result on extraction failure, preserve batch totals and undo history, and save per-file page counts for later continuation.
- Add offline regression coverage for single-file reanalysis, stopping, source changes, recovery guards, saved page counts, and correction/undo integration. Real Gemini/PDF acceptance remains deferred.

## 1.1.0 — 2026-09-06

### Batch processing and review

- Automatically rename files with complete required metadata and an available destination. Keep missing metadata, extraction errors, and name conflicts visible for review.
- Review all analyzed PDF pages alongside editable metadata, with page navigation, zoom, and optional model-provided source quotations.
- Correct metadata or filenames locally, including files already renamed in the current batch.
- Show distinct-file progress as `Renamed: X / total` and `Analyzed: Y / total`; corrections and undo update the counters without counting a file twice.
- Retry extraction failures without rerunning successful or manually corrected results.

### Recovery and file safety

- Record renames in durable undo journals; check file contents and refuse to overwrite occupied destinations.
- Save the latest batch, unstarted files, metadata, settings, and exact analyzed PDF prefixes after each completed file.
- Restore saved results on startup without model requests or automatic renames. Continue pending work with cached metadata when source contents match.
- Reanalyze changed files and hold missing or ambiguous paths for recovery. Recover interrupted rename/undo state from recorded intent and actual file contents.
- Fix cancellation callback compatibility and retain completed work when processing stops.

### Packaging and validation

- Embed version 1.1.0 in the app and Windows executable properties. Provide `PaperDF-v1.1.0-windows.exe` and a SHA-256 checksum through a shared build command.
- Include PDF rendering dependencies in release builds and require release tags/manual version inputs to match `VERSION.txt`.
- Add offline workflow, extraction, UI, retry, and recovery tests with Windows/Linux/macOS CI configuration.
- Local offline validation: 110 tests, 109 passed and one Windows symlink-permission skip. Real Gemini/PDF acceptance remains deferred; remote CI and macOS/Linux builds are not claimed as verified.

Only the latest batch is restored. Starting another batch replaces its saved results and generated review cache; undo history remains separate. A provider response lost before its checkpoint can require another extraction request.
