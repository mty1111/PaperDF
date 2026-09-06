"""Conservative academic names, local naming styles, and version-date policy."""
import re
from titlecase import titlecase

SUFFIXES = {'jr', 'jr.', 'sr', 'sr.', 'ii', 'iii', 'iv'}
PARTICLES = {'van', 'von', 'de', 'del', 'della', 'di', 'da', 'dos', 'das', 'du', 'der', 'den', 'la', 'le', 'ter', 'ten', 'al', 'bin', 'ibn'}
AUTHOR_KINDS = ('person', 'organization', 'unknown')
DOCUMENT_KINDS = ('published_article', 'preprint', 'book', 'unknown')
DATE_KINDS = ('publication', 'online', 'revision', 'preprint', 'copyright', 'original', 'received', 'accepted', 'accessed', 'other')
DEFAULT_ALIASES = 'AER = American Economic Review\nJPE = Journal of Political Economy\nQJE = The Quarterly Journal of Economics'
PROTECTED = {word.casefold(): word for word in ('AI', 'GDP', 'GNP', 'DSGE', 'IV', 'OLS', 'GMM', 'VAR', 'ARIMA', 'DNA', 'RNA', 'COVID-19', 'pH', 'LaTeX', 'eBay', 'iPhone', 'OECD', 'IMF', 'NBER', 'EU', 'UK', 'US', 'USA')}


def compact(text):
    return ' '.join(text.split())


def parse_name(full):
    """Fallback for old/manual names; explicit structured family names win."""
    raw = compact(full)
    empty = {'first': '', 'middle': '', 'surname': '', 'suffix': ''}
    if not raw:
        return empty
    comma = [part.strip() for part in raw.split(',') if part.strip()]
    if len(comma) > 1 and comma[1].casefold() not in SUFFIXES:
        family, rest = comma[0], ' '.join(comma[1:]).split()
        suffix = rest.pop() if rest and rest[-1].casefold() in SUFFIXES else ''
        return dict(first=rest[0] if rest else '', middle=' '.join(rest[1:]), surname=family, suffix=suffix)
    parts = raw.replace(',', ' ').split()
    suffix = parts.pop() if parts and parts[-1].casefold() in SUFFIXES else ''
    if not parts:
        return dict(empty, suffix=suffix)
    index = len(parts) - 1
    while index > 1 and parts[index - 1].casefold() in PARTICLES:
        index -= 1
    return dict(first=parts[0] if index else '', middle=' '.join(parts[1:index]),
                surname=' '.join(parts[index:]), suffix=suffix)


def author_components(full, details):
    detail = next((item for item in details if compact(item.get('literal', '')) == compact(full)), None)
    if detail:
        if detail['kind'] == 'organization':
            return None  # render literal regardless of personal-name template
        if detail['kind'] == 'unknown' or not detail.get('family'):
            return None
        given = detail.get('given', '').split()
        return dict(first=given[0] if given else '', middle=' '.join(given[1:]),
                    surname=detail['family'], suffix=detail.get('suffix', ''))
    # Clear institutional cues help legacy rows; unknown non-Latin mononyms stay whole.
    if re.search(r'\b(university|institute|bank|organization|organisation|committee|consortium|collaboration|commission|bureau|department|council|society|association)\b', full, re.I):
        return None
    return parse_name(full)


def title_style(text, style='preserve'):
    text = compact(text)
    if style == 'preserve':
        return text
    if style != 'title':
        raise ValueError('Unknown title style.')
    all_caps = text.isupper()
    def protect(word, **kwargs):
        core = word.strip('()[]{}:;,!?"')
        if core.casefold() in PROTECTED:
            return word.replace(core, PROTECTED[core.casefold()])
        if not all_caps and (core.isupper() or any(c.isupper() for c in core[1:])):
            return word
        if any(c.isdigit() for c in core) or '\\' in core or '$' in core:
            return word
        return None
    return titlecase(text, callback=protect)


def alias_key(value):
    # Exact normalized matching only: no fuzzy expansion of similar journals.
    return re.sub(r'[\s.]', '', value).casefold()


def parse_aliases(text):
    result = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        if '=' not in line:
            raise ValueError('Journal aliases must use alias = full journal name, one per line.')
        alias, canonical = (compact(value) for value in line.split('=', 1))
        key = alias_key(alias)
        if not key or not canonical:
            raise ValueError('Journal aliases require both an alias and a full name.')
        if key in result and result[key] != canonical:
            raise ValueError('Conflicting journal alias: ' + alias)
        result[key] = canonical
    return result


def canonical_journal(value, aliases, is_book=False):
    return value if is_book else parse_aliases(aliases).get(alias_key(value), value)


def select_year(academic):
    """Return (year, explanation); an empty year requires document review."""
    kind = academic.get('document_kind', 'unknown')
    dates = academic.get('dates', [])
    priorities = {'published_article': ('publication', 'online'), 'preprint': ('revision', 'preprint'),
                  'book': ('publication',), 'unknown': ()}[kind]
    for category in priorities:
        years = sorted({item['year'] for item in dates if item['kind'] == category})
        if not years:
            continue
        if category == 'revision':
            return years[-1], 'Latest reported revision year.'
        if len(years) == 1:
            return years[0], f'Year from {category} date.'
        return '', f'Conflicting {category} years; choose the year for this version.'
    return '', 'No unambiguous date for this document version; choose a year after review.'


def academic_review_reasons(metadata):
    academic = metadata.get('academic')
    if not academic:
        return []  # old saved rows keep their established review state
    reasons = []
    if not select_year(academic)[0]:
        reasons.append(select_year(academic)[1])
    if any(item['kind'] == 'unknown' or (item['kind'] == 'person' and not item['family'])
           for item in academic['author_details']):
        reasons.append('Author type or family name needs review.')
    return reasons
