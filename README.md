# PaperDF — Paper Document Formatter

**PaperDF** renames large batches of academic PDFs using AI-extracted metadata from the first pages.  
It reads a small snippet of each file, asks Gemini to extract **Authors / Year / Journal (or Publisher) / Title**, and renames the file according to your templates. It handles both papers and books, supports scanned PDFs, and avoids duplicate/unsafe filenames.

> Get a Gemini API key: **https://aistudio.google.com/apikey**  
> Gemini model catalog: **https://ai.google.dev/gemini-api/docs/models**

---

## Why this exists

Papers downloaded from the web often have unreadable filenames (e.g., `s2-3453245-main.pdf`). Manual renaming is slow because titles/authors are not always copy-pastable from the PDF. **PaperDF (Paper Document Formatter)** offloads the extraction to **Gemini**, which can parse PDFs—including many scanned documents—at low cost (on the order of **a few euro cents for hundreds of files**, subject to provider pricing and usage).

---

## Key features

- **AI metadata extraction (Gemini):** reads **only the first N pages** per file; sends that snippet to Gemini for structured JSON.
- **Two modes:** paper vs. book. For books, “journal” is treated as **publisher**.
- **Custom filename templates:** separate templates for papers vs. books.
- **Author rendering rules:** separate **author-format** options for papers vs. books (e.g., `"{surname}"` for papers, `"{surname}, {first_initial}."` for books).
- **Embedded Cheatsheet (in Settings):** quick reference for all tokens and examples.
- **Expanded Help (in Config → Help…):** how it works, end-to-end usage, rename logic, and troubleshooting.
- **Duplicate handling:** robust collision resolution, content-hash checks, and “already formatted” skip.
- **First-run setup guide:** appears automatically if no config exists.
- **Default model:** `gemini-2.5-flash-lite` (you can change it in Settings).

---

## Installation

**Requirements**
- Python 3.9+ (tkinter included on most platforms; on Linux you may need `python3-tk`)
- Packages:
  - `google-genai` (Google AI Studio SDK)
  - `PyPDF2`
  - `python-dotenv`
  - `titlecase` (optional; falls back to `str.title()` if missing)

**Install packages**
```bash
pip install google-genai PyPDF2 python-dotenv titlecase
```
> If tkinter is missing on Linux: `sudo apt-get install python3-tk`

---

## Releases (standalone)

Prefer a one-click setup? Download the **standalone build** from the **GitHub Releases** page of this repository.

- No Python or dependencies required.
- Just run the single-file app (e.g., on Windows, a `.exe` named **PaperDF.exe**).
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
2. It uploads that small snippet to Gemini and requests **strict JSON**:
   - `authors: []`, `year: "..."`, `journal: "..."`, `title: "..."`.
   - For **books**, `journal` is interpreted as the **publisher**.
3. It builds a filename using your template:
   - Papers default: ``{journal} - {year} - {authors} - {title}.pdf``
   - Books default: ``{authors} - {title} - {journal} ({year}).pdf``
4. It cleans invalid characters, checks for duplicates, and renames safely.

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
   - Increase if your PDFs have long prefaces or delayed title pages.

4. **Start**
   - Click **Start**. Use **Abort** to stop.  
   - The **Log** panel shows each decision (skips, outputs, errors).

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

- **Already formatted?**  
  A strict validator checks if the filename already matches your current pattern and author format → **skip**.

- **Empty metadata?**  
  If nothing meaningful is extracted → **skip**.  
  If **Book mode** and no `title` → **skip** (to avoid junk names).

- **Collisions & duplicates**  
  - If the target filename already exists:
    - If contents are identical (SHA-1 hash) → **skip**.  
    - Otherwise append a short hash suffix: `[…]` for uniqueness.

---

## Notes on cost and privacy

- Only the **first N pages** are sent to the model.  
- Costs are typically low (on the order of **a few euro cents per hundreds of files**), but depend on provider pricing and usage.  
- Works well for many **scanned PDFs**; OCR quality still matters.

---

## Troubleshooting

- **“Gemini API key is required.”**  
  Set your key in **Config → Settings…** (or `.env` file as described).

- **“Invalid JSON”** from the model  
  Increase `Pages to extract`; ensure early pages contain title/author info.

- **Repeated “Skipped (same name)”**  
  Your template currently evaluates to the existing filename.

- **Unexpected publisher/journal**  
  For books, `{journal}` is intentionally treated as the **publisher**.

- **Odd author rendering**  
  Adjust **Author Format**; confirm token usage; unusual name orders may need manual fixes.

---

## Roadmap (suggested)

- Presets dropdown for common author styles (e.g., “Surname, F.”).  
- Batch overrides (e.g., per-file page count).  
- Optional dry-run with CSV report.

---

## License

MIT. See `LICENSE`. Adapt as needed.

---

## Attribution

- **Google Gemini** (via **Google AI Studio**) is used at runtime for metadata extraction from PDF snippets.  
  - API key: https://aistudio.google.com/apikey  
  - Model catalog: https://ai.google.dev/gemini-api/docs/models
- **ChatGPT** assisted in the software design and documentation.
- Libraries and tooling: `google-genai`, `PyPDF2`, `tkinter`, `configparser`, `python-dotenv`, `titlecase`.
