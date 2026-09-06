# Academic metadata rules (1.3.0)

Stage 3 adds structured names, conservative capitalization, configurable journal aliases and an explicit document-version year policy. These rules operate on the analyzed first N PDF pages. They do not establish factual accuracy of the model's extraction.

## Names and institutions

New responses include one `author_details` entry per author: the original `literal`, `kind` (`person`, `organization`, `unknown`), `given`, full `family`, and `suffix`. The strict validator checks alignment and types. Organizations and unknown names must have empty personal parts.

- `{surname}` and `{family}` use the entire structured family name: Gabriel García Márquez → García Márquez; Charles de la Vallée Poussin → de la Vallée Poussin.
- Organizations keep their literal name under every personal-name template: World Bank stays World Bank.
- Accents, particles, hyphens and source capitalization are preserved. Jean-Paul Sartre with `{surname}, {first_initial}.` becomes Sartre, J.-P. Include `{suffix}` to render Jr. or another suffix.
- Unknown author types or missing personal family names stop automatic renaming and appear as Needs review. The full literal name remains available.

In **Review document... → Name parts...**, select an author and correct Type, Given names, Full family name and Suffix. **Use these names** updates only the open review dialog; **Apply correction** applies and saves it. Closing the document dialog without applying discards these edits. Editing the author list realigns matching entries; new names begin as unknown until their parts are set. No model request is made.

Old saved results without structured details remain readable and use fallback parsing for comma-separated family-first names, common particles and suffixes. A plain unmarked name cannot reliably identify every compound surname or naming culture; use Name parts when needed. Model-supplied parts also need checking when uncertain.

## Capitalization

**Settings → Title capitalization** defaults to `preserve`: retain extracted case while normalizing whitespace. Names and journal/publisher text also retain extracted case.

The optional `title` style formats titles locally and protects known acronyms (such as GDP and DSGE), case-sensitive terms (pH, LaTeX, eBay), mixed-case words, numeric tokens and marked math tokens. It is not a universal scientific tokenizer: an unfamiliar acronym in an ALL-CAPS title can still be changed. Keep `preserve` or edit the title in that case. Filesystem-invalid characters are still removed when constructing filenames.

## Journal aliases

**Settings → Journal aliases** accepts one `alias = full journal name` per line. Clear the field to disable expansion. Duplicate aliases with conflicting destinations are rejected. Matching ignores case, whitespace and periods, but uses no fuzzy matching. For example, A.E.R. matches AER; AER: Insights does not. Aliases affect paper filenames only, leaving source metadata and book publishers unchanged.

The small built-in registry uses publisher-confirmed journal names:

| Alias | Filename form | Publisher source |
| --- | --- | --- |
| AER | American Economic Review | [American Economic Association](https://www.aeaweb.org/journals/aer) |
| JPE | Journal of Political Economy | [University of Chicago Press](https://www.journals.uchicago.edu/toc/jpe/current) |
| QJE | The Quarterly Journal of Economics | [Oxford University Press](https://academic.oup.com/qje/pages/About) |

Custom mappings can extend or replace this registry. It is deliberately not a comprehensive abbreviation database.

## Document-version years

New responses identify a `document_kind` and date candidates. Each candidate has a date `kind`, four-digit year, physical PDF page and quoted passage. Validation checks page bounds, quote length and that the year occurs in the quote. It does not independently verify the quote against the PDF.

The app applies this policy, rather than claiming a universal citation standard:

| Document | Preferred date | If uncertain |
| --- | --- | --- |
| Published article | Publication year, otherwise online year | Conflicting years in the preferred category require review. |
| Preprint / working paper | Latest reported revision year, otherwise preprint year | Conflicting initial preprint years require review. |
| Book | Publication year of the current edition | Copyright or original-edition year alone requires review. |
| Unknown | No automatic year | Review the document. |

Received, accepted, accessed, copyright and original-edition dates are retained as context, not automatically selected. The prompt excludes reference-list dates and dates belonging to other versions from publication candidates. A model can still misclassify a date, and dates outside the analyzed pages are unavailable.

The review dialog shows the suggested year and its reason, a dropdown of reported dates, and quoted sources. Choosing a reported year fills the Year field and jumps to its PDF page. The Year field remains editable for manual decisions; changes apply only through **Apply correction** and remain undoable. Conflicting or unsupported dates leave the original file unchanged until reviewed.

## Cache, saved batches and verification

Extraction rules now use `gemini-academic-structured-v2`. New processing does not reuse pre-1.3 extraction entries. Existing saved batches remain readable; they retain their metadata and manual choices. New batches freeze capitalization and alias settings for retry/continuation. Old saved naming settings default to preserve case and no alias expansion. Changing local naming styles uses cached structured metadata without another extraction. Manual corrections remain local to the saved batch.

Offline verification uses synthetic PDFs and mocked provider responses: compound names, institutional authors, case-sensitive titles, aliases, competing date categories, schema failures, cache reuse, saved settings, review-page navigation, dialog cancellation, automatic rename, correction and undo. The Windows local run executes 155 tests: 154 pass and one symlink test skips for host permissions. Cross-platform CI and release builds are recorded on the matching commit/tag. Real Gemini/PDF acceptance remains deferred by user request.
