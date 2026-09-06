# PaperDF roadmap status

## Current delivery: 1.2.0 pre-release

Stage 2 implementation is complete; real Gemini/PDF acceptance remains deferred by user request. CI and release outcomes are recorded on the matching GitHub commit/tag; do not infer installation or real-provider validation from a successful package build.

| Stage | Status | Scope |
| --- | --- | --- |
| 1: inspect and recover | Implemented with revised interaction | Metadata result table, multipage PDF review, local corrections and durable undo. Automatic normal renames replaced the originally proposed mandatory checkbox approval flow. |
| 2: avoid redundant requests and incorrect skips | Implemented; offline regression verified locally | See completion checklist below. |
| 3: academic metadata details | Not systematically implemented | Compound surnames, institutional authors, acronym/title preservation, journal aliases and version-year policy remain. |
| 4: architecture and expansion | Partial module separation | Workflow, session, review, cache and schema modules exist. CLI, other providers and local metadata extraction remain. |

## Stage 2 completion checklist

- Cross-batch persistent result cache plus timestamped attempt records: content SHA-256, requested page count, paper/book mode, provider/model and versioned extraction rules.
- Reuse after renames/moves/restarts, with exact analyzed PDF-prefix bytes and hashes. New naming settings do not trigger another extraction. New batches do not erase the long-term cache.
- No filename-only skip; changed contents/settings miss cache. Cache hits still pass normal completeness and safe rename policy. Manual corrections remain local to the saved batch.
- Cache-only processing does not require credentials or a provider client. Lazy connection failures leave uncached files retryable.
- Exact local response validation: required/unknown/duplicate keys, field types, year format, evidence structures, quote lengths and physical page bounds. Empty fields are held for review; malformed responses are errors, never cache successes.
- Failed-file retry and saved continuation use cache policy. Forced refresh survives pending/failed files and restart; single-file reanalysis always refreshes.
- Transactional cache writes, content checks before committing results, damaged-cache reporting, cache-source indicators, and explicit clearing of cache/attempt records.

## Limits and recovery

- Provider responses lost before the cache commit can require another request. Separate simultaneously running app instances may race to request the same uncached PDF; there is no distributed request lock.
- Model identifiers are cache keys; provider-side updates under an unchanged identifier require explicit refresh. Extraction rule changes must bump `EXTRACTION_RULES_VERSION`.
- Entries persist until cleared. Config clearing preserves latest-batch state and rename journals. If the database itself cannot be opened, close the app and move `extraction-cache.sqlite3` aside to preserve it before retrying.
- The latest saved batch keeps original extraction settings and local corrections; strict response validation applies to new model responses, not retroactively to saved/manual metadata.

Local baseline verification: `python scripts/run_tests.py` — 141 tests, 140 passed, one Windows symlink-permission skip. Tests use synthetic PDFs and mocked provider responses. Publication and remote CI must be checked separately.
