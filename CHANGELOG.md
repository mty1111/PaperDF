# Changelog

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
