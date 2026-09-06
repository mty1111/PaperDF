# PaperDF — Paper Document Formatter

Current source version: **1.1.0**. See [CHANGELOG.md](CHANGELOG.md) for changes and validation scope.

**PaperDF** renames large batches of academic PDFs using AI-extracted metadata from the first pages.  
It reads the first several pages of each file, asks Gemini to extract **Authors / Year / Journal (or Publisher) / Title**, and renames files according to your templates. Files needing attention stay unchanged. When you want to check a result, read the same analyzed pages alongside its metadata, correct it locally, and apply the correction. Any rename batch can be undone.

> Get a Gemini API key: **https://aistudio.google.com/apikey**  
> Gemini model catalog: **https://ai.google.dev/gemini-api/docs/models**

---

## Why this exists

Papers downloaded from the web often have unreadable filenames (e.g., `s2-3453245-main.pdf`). Manual renaming is slow because titles/authors are not always copy-pastable from the PDF. **PaperDF (Paper Document Formatter)** uses **Gemini** to extract metadata from PDFs—including scanned documents—and handles ordinary files automatically. Titles, authors, and publication details can appear across several pages, so optional review covers the entire analyzed prefix rather than only the first page.

---

## Key features

- **AI metadata extraction (Gemini):** reads **only the first N pages** per file; sends that snippet to Gemini for structured JSON.
- **Two modes:** paper vs. book. For books, “journal” is treated as **publisher**.
- **Custom filename templates:** separate templates for papers vs. books.
- **Author rendering rules:** separate **author-format** options for papers vs. books (e.g., `"{surname}"` for papers, `"{surname}, {first_initial}."` for books).
- **Embedded Cheatsheet (in Settings):** quick reference for all tokens and examples.
- **Expanded Help (in Config → Help…):** how it works, end-to-end usage, rename logic, and troubleshooting.
- **One-click processing:** automatically rename files with a title, authors, and a four-digit year. Missing journal/publisher information is allowed.
- **Retry failed files:** retry extraction errors in the current results without rerunning completed files. Fix the API key or model in Settings, then retry with the batch's original page count, mode, and naming settings.
- **Continue after restarting:** the latest batch, metadata, and analyzed PDF pages are saved locally. Restore results on startup and use **Continue batch** to finish pending work, reusing unchanged files' metadata.
- **Results organized by metadata:** inspect titles, authors, years, and status; use **Needs attention only** to focus on incomplete results or errors.
- **Optional document review:** open any result, including an already renamed file, and browse all the pages used for extraction alongside editable metadata. Flip pages, jump to a page, or zoom.
- **Source locators:** when Gemini returns source pages and quoted passages, use them to find metadata in the document. These are model-provided navigation aids, not independently verified evidence.
- **Local corrections:** edit metadata or enter a filename override without another model request. Each applied correction has its own undo batch.
- **Batch undo:** a persistent local journal allows undoing applied renames after restarting the app.
- **File safety:** content-hash checks, no overwriting, and conflicts held for review before applying a distinct filename.
- **First-run setup guide:** appears automatically if no config exists.
- **Default model:** `gemini-2.5-flash-lite` (you can change it in Settings).

---

## Installation

**Requirements**
- Python 3.9+ (tkinter included on most platforms; on Linux you may need `python3-tk`)
- Packages:
  - `google-genai` (Google AI Studio SDK)
  - `PyPDF2`
  - `pypdfium2` and `Pillow` (in-app PDF page rendering)
  - `python-dotenv`
  - `titlecase` (optional; falls back to `str.title()` if missing)

**Install packages**
```bash
pip install -r requirements.txt
```
> If tkinter is missing on Linux: `sudo apt-get install python3-tk`

---

## Releases (standalone)

Prefer a one-click setup? Download the **standalone build** from the **GitHub Releases** page of this repository.

- No Python or dependencies required.
- Just run the single-file app (e.g., **PaperDF-v1.1.0-windows.exe** on Windows).
- On first launch, open **Config → Settings…**, paste your **Gemini API key**, review templates, and save.
- Everything else works the same as the source version.

> Note: Your OS may warn about unsigned executables. If prompted, allow running the app you downloaded from the official release of this repo.

---

## Getting an API key

Create a key at **https://aistudio.google.com/apikey** and keep it private.

The program will ask for the key in **Config → Settings…** on first run.  
Optionally place it in `pdf_metadata_renamer.env` under the app’s config folder:
```ini
GEMINI_API_KEY=your_key_here
```

---

## Model selection

The default model is **`gemini-2.5-flash-lite`**. You can set any available Gemini model in **Config → Settings…**.  
See the current model catalog here: **https://ai.google.dev/gemini-api/docs/models**.

---

## Running

```bash
python pdf_metadata_renamer.py
```

On first launch, a **Setup Guide** will open:
1. Open **Config → Settings…**  
2. Paste your **Gemini API key**  
3. Review **filename templates** and **author formats**  
4. Save

---

## How it works (concise)

1. The app reads **only the first N pages** (`Pages to extract`) of each PDF.
2. It uploads that snippet to Gemini and requests structured JSON metadata:
   - `authors: []`, `year: "..."`, `journal: "..."`, `title: "..."`.
   - For **books**, `journal` is interpreted as the **publisher**.
   - Optional source page numbers and quoted passages help you locate individual fields.
3. It builds a filename using your template:
   - Papers default: ``{journal} - {year} - {authors} - {title}.pdf``
   - Books default: ``{authors} - {title} - {journal} ({year}).pdf``
4. It cleans invalid characters and checks metadata, source contents, and destination names. Files with a title, authors, and a four-digit year are renamed automatically when the destination is available. Missing journal/publisher information uses your unpublished placeholder.
5. Incomplete metadata, extraction failures, and filename conflicts stay unchanged and appear in the results. You can review any result against the exact pages sent to Gemini, correct it locally, and apply the correction. A local journal records applied changes for undo.

Every **Process PDFs** run reads PDF content and requests metadata; a filename that already looks formatted is not enough to skip extraction. **Retry failed files** rereads only files whose extraction failed and makes new extraction attempts. Opening **Review document...**, browsing pages, applying corrections, and **Undo last batch** work locally and make no model requests. Complete metadata is not a guarantee of factual accuracy; review is available for successful results too.

---

## Usage workflow

1. **Select input**
   - **Browse Files** to pick specific PDFs, or  
   - **Browse Folder** to process all PDFs under a folder (recursive).  
   *Use one mode at a time.*

2. **Choose mode**
   - Toggle **Book mode** if processing books (uses book template + book author format).

3. **Set `Pages to extract`**
   - How many first pages to read for metadata.  
   - Defaults: **4** (papers) and **20** (books when Book mode is ON).  
   - Increase if your PDFs have long prefaces or delayed title pages; the allowed range is **1–50**.

4. **Process PDFs**
   - Click **Process PDFs** to extract metadata and automatically rename eligible files. You do not need to approve each filename first.
   - Use **Stop** to request cancellation between files. An in-flight request finishes first; already completed renames remain undoable.
   - Live counters show **Analyzed: X / total** and **Renamed: Y / total files**. Renamed counts distinct files whose names currently differ from their original names; corrections and undo update the count, and stopping after analysis begins preserves the full batch total.
   - Read the results by title, authors, year, and status. Enable **Needs attention only** to focus on files requiring action. The **Log** panel records extraction and rename outcomes.

5. **Retry extraction failures when needed**
   - **Retry failed files** becomes available when the current results contain extraction errors, such as PDF read failures, network/API errors, invalid responses, or Gemini client setup failures. Completed or undone results, incomplete metadata, filename conflicts, and local rename failures are not retried.
   - Correct your API key or model in **Config → Settings…** if needed, then click **Retry failed files**. Retry uses those current connection settings and the original batch's page count, paper/book mode, and naming settings.
   - The full batch total and count of distinct analyzed files remain intact. A separate **Retrying: X / failed count** shows progress through the retry; repeated attempts do not count as additional analyzed files. Eligible recovered files are renamed automatically in a separate undo batch. Files that fail again remain retryable.
   - **Stop** takes effect between files. The in-flight extraction finishes, but stopping prevents automatic renaming of recovered results. Finish those files with **Continue batch** or **Review document...**; unattempted failures remain retryable.

6. **Continue a saved batch**
   - The app saves the complete file list before extraction, then checkpoints each extraction, rename, correction, and undo. Closing the app preserves the latest batch, including files not yet started.
   - On startup, results and counters are restored while source contents are checked. Startup makes no model requests and performs no renames. Click **Continue batch** when ready.
   - Unchanged files with complete cached metadata finish locally without another API request or API key. Pending files, extraction errors, and files marked **Changed** receive fresh extraction using the saved page count/mode/naming settings and your current API key/model. Existing completed and undone results stay completed unless their contents changed.
   - Files marked **Missing** stay unchanged: restore them to the displayed path, then continue. The app does not guess where an externally moved file went. **Recovery needed** means an interrupted rename cannot be verified; resolve the reported paths or use **Undo last batch**.
   - Metadata gaps and filename conflicts still require document review. Newly applied renames form a separate undo batch. The full file total remains fixed, and repeated extraction attempts do not inflate the analyzed count.
   - Only the latest batch is restored. Starting a new **Process PDFs** batch replaces the saved batch and removes its old generated review cache; the undo history remains independent. Changes to files during folder enumeration are outside the saved manifest: continuation uses the discovered file list, not a fresh folder scan.
   - Checkpoints cover completed extraction attempts. If the app terminates after a provider response but before that result is saved, continuing may repeat that request.

7. **Review a document when needed**
   - Double-click any result or select it and choose **Review document...**. Successful renamed files can be reviewed too.
   - Browse the same saved PDF prefix used for extraction. It contains up to your configured N pages, or the whole document if shorter. Flip through every page, jump to a page, and zoom; the viewer is not limited to the first page.
   - Source quotations and **PDF page N** buttons, when available, help locate authors, year, journal/publisher, and title. Page numbers count PDF pages starting at 1, including covers and front matter; they are not the page labels printed inside the document.
   - Correct authors (one per line), year, journal/publisher, and title alongside the PDF. Missing source locators do not block review or correction.

8. **Apply a correction**
   - Use **Apply correction** to save the edited metadata and apply the resulting name locally. An optional filename override must include `.pdf`.
   - Each correction that renames a file creates a separate undo batch. Correcting an already renamed file works from its current path.
   - A filename conflict is held for review. A distinct name with a short hash suffix can be proposed in the review window; inspect and accept it or enter your own filename.
   - If the corrected destination is occupied, the file stays unchanged and needs review. Open **Review document...** again to inspect the newly suggested distinct name or override it. Existing files are never overwritten.
   - The app rechecks source contents before renaming. **Continue batch** reanalyzes changed files before applying a new name. Starting a new **Process PDFs** run also analyzes current contents.

9. **Undo last batch**
   - Use **Undo last batch** to restore names from the latest batch with outstanding changes, including after restarting the app.
   - Undo refuses to overwrite an occupied original path or restore a file whose contents have changed. Resolve the reported issue before retrying.
   - Review follows each restored file to its original path. The latest batch's saved results preserve these paths across restarts.
   - History is stored in `rename-history` under the app's config directory (shown in **About**). On Windows this is normally `%LOCALAPPDATA%\pdfrenamer\rename-history`; without `LOCALAPPDATA`, it is `~/pdfrenamer/rename-history`.

---

## Settings (Config → Settings…)

- **Output Pattern (papers)**  
  Template for non-book files. Default:  
  ``{journal} - {year} - {authors} - {title}.pdf``

- **Book Output Pattern**  
  Template for books. Default:  
  ``{authors} - {title} - {journal} ({year}).pdf``  
  *Here `{journal}` means the **publisher**.*

- **Unpublished Placeholder**  
  Used when `{journal}` (or publisher) is missing. Default: `Unpublished`.

- **Author Format (papers)**, **Author Format (books)**  
  Template to render **each author** before joining with “, ”.  
  Common choices:
  - Papers: ``{surname}``
  - Books: ``{surname}, {first_initial}.``

  **Author tokens**
  ```
  {first} {middle} {surname}  (aliases: {last}, {family})
  {first_initial} {middle_initials} {surname_initial} {suffix}
  ```
  *Punctuation is literal; include commas/dots where you want them.*

- **Gemini API Key**  
  Stored locally in your user config directory.

- **Model**  
  Defaults to `gemini-2.5-flash-lite`. You can override if needed.

- **Cheatsheet (embedded in Settings)**  
  A compact reference of tokens, defaults, and examples.

---

## Filename tokens (for patterns)

```
{journal}  -> journal or publisher (book mode)
{year}     -> "n.d." if missing
{authors}  -> rendered from your author format, authors joined by ", "
{title}
```

**Author tokens** (used *inside* author formats):
```
{first} {middle} {surname}
{last} {family} (aliases of {surname})
{first_initial} {middle_initials} {surname_initial} {suffix}
```

---

## Rename logic & safety

- **Same resulting name?**
  Metadata is extracted during processing even if the current filename looks formatted. If the resulting target name is unchanged, no rename is needed.

- **Incomplete metadata or extraction failure?**
  Automatic renaming requires a nonempty title, at least one author, and a four-digit year in both modes. Missing journal/publisher information alone is allowed, including for working papers. Missing required fields or failed extraction leaves the file unchanged for review. Correct the metadata or enter a filename override to apply a manual correction.

- **Collisions & duplicates**  
  - If the target filename already exists:
    - If contents are identical (SHA-256 hash) → **skip**.
    - Otherwise leave the file unchanged for review. A short hash suffix, such as `_1a2b3c4d`, can be suggested for you to inspect and accept in the review window.
  - If several files would use the same destination in a batch, the first eligible file may be renamed and later conflicting files are held for review.
  - Applying a rename rechecks source contents and refuses occupied destinations without overwriting them or silently choosing another name.

- **Recoverable batches**
  Applied renames are recorded in a persistent journal. Undo checks file contents and original paths before restoring names; unsuccessful entries remain retryable.

---

## Notes on cost and privacy

- Only the **first N pages** are sent to the model during **Process PDFs**, **Retry failed files**, or **Continue batch** when fresh extraction is needed. Reusing cached metadata makes no model request. The app attempts to delete each uploaded Gemini snippet when the extraction attempt finishes, including after errors.
- Costs depend on the selected model, document content, page count, and provider pricing. No benchmark or cost estimate is included here.
- Scanned PDFs can be processed, but scan quality and model extraction errors can affect metadata and source locators. Source pages and quotations are returned by the model and are not independently fact-checked.
- The latest batch's file paths, content hashes, metadata, naming settings, progress, and exact analyzed PDF prefixes persist locally under `batch-state` in the app storage directory (normally `%LOCALAPPDATA%\pdfrenamer` on Windows). API credentials are not part of the saved batch settings. Temporary working copies are cleaned up on normal exit; durable review copies survive restarting.
- Starting another batch replaces the saved results and removes the previous batch's generated review cache. To remove saved results without processing another batch, close the app and remove its `batch-state` folder. The separate `rename-history` folder retains undo records.
- Browsing pages, editing, applying corrections, and undoing results are local operations and do not send another model request.

---

## Troubleshooting

- **“Gemini API key is required.”**  
  Set your key in **Config → Settings…** (or `.env` file as described).

- **Network/API errors or invalid model responses**
  Check the connection and API key/model in **Config → Settings…**, then use **Retry failed files**. Retry preserves the original page count; to analyze more pages, increase `Pages to extract` and start a new **Process PDFs** run.

- **Repeated “Unchanged” status**
  Your template currently evaluates to the existing filename.

- **Unexpected publisher/journal**  
  For books, `{journal}` is intentionally treated as the **publisher**.

- **Odd author rendering**  
  Adjust **Author Format**; confirm token usage; unusual name orders may need manual fixes.

- **Source changed or destination occupied**
  Use **Continue batch** to reanalyze a changed source. For an occupied destination, open **Review document...** and inspect the suggested distinct filename or enter an override before applying a correction. Existing files are not overwritten.

- **Source page or quotation is missing or incorrect**
  Browse all the analyzed pages to check the metadata. Source locators are optional model output; they may be absent or mistaken. If relevant information is outside the snippet, increase `Pages to extract` and process the file again.

- **Saved batch cannot be restored or saved**
  Check the displayed `batch-state` path and available disk space. An unreadable or unsupported checkpoint is reported and kept in place, rather than silently discarded. Earlier versions did not save results, so batches processed with those versions must be processed again. **Undo last batch** still uses the independent rename history.

- **Missing file after restarting**
  Restore the file to the path shown in the result, then click **Continue batch**. Cached metadata is reused only if the content hash still matches.

- **Undo cannot restore a file**
  Check whether the renamed file was modified, moved, or removed, or whether the original path is now occupied. The failed entry remains in the journal for retry.

---

## Development checks

Run the local test suite from the repository root:

```bash
python -m unittest discover -s tests -v
```

Build the versioned Windows executable from the repository root:

```powershell
python -m pip install -r requirements.txt pyinstaller
python scripts/build_windows.py
```

This creates `dist/PaperDF.exe`, `dist/PaperDF-v1.1.0-windows.exe`, and its `.exe.sha256` checksum. The app embeds `VERSION.txt`; Windows file properties use `version_info.txt`. Update both version files and `CHANGELOG.md` when preparing a new version. The build refuses mismatched version metadata.

---

## Roadmap (suggested)

- Presets dropdown for common author styles (e.g., “Surname, F.”).  
- Batch overrides (e.g., per-file page count).  
- CSV export of processing results.

---

## License

MIT. See `LICENSE`. Adapt as needed.

---

## Attribution

- **Google Gemini** (via **Google AI Studio**) is used at runtime for metadata extraction from PDF snippets.  
  - API key: https://aistudio.google.com/apikey  
  - Model catalog: https://ai.google.dev/gemini-api/docs/models
- **ChatGPT** assisted in the software design and documentation.
- Libraries and tooling: `google-genai`, `PyPDF2`, `pypdfium2`, `Pillow`, `tkinter`, `configparser`, `python-dotenv`, `titlecase`.
