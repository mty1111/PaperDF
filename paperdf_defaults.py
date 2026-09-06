"""Shared defaults; importing them never reads user configuration."""
DEFAULT_MODEL = 'gemini-2.5-flash-lite'   # default model

INVALID_FILENAME_CHARS = '<>:"/\\|?*'
DEFAULT_OUTPUT_PATTERN = '{journal} - {year} - {authors} - {title}.pdf'
# Default book template as requested
DEFAULT_BOOK_OUTPUT_PATTERN = '{authors} - {title} - {journal} ({year}).pdf'
DEFAULT_UNPUBLISHED = 'Unpublished'
DEFAULT_PAPER_PAGES = 4
DEFAULT_BOOK_PAGES = 20
MAX_PAGES_TO_EXTRACT = 50

# Author format defaults
DEFAULT_AUTHOR_FMT_PAPER = '{surname}'
DEFAULT_AUTHOR_FMT_BOOK = '{surname}, {first_initial}.'
