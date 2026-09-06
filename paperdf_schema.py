"""Local validation of the provider contract, independent of SDK enforcement."""
import json
import re
from paperdf_academic import AUTHOR_KINDS, DOCUMENT_KINDS, DATE_KINDS, compact

FIELDS = ('authors', 'year', 'journal', 'title')


def validate_metadata(data, page_count, *, normalized=False):
    def fail(detail):
        raise ValueError('Invalid metadata schema: ' + detail)
    expected = set(FIELDS) | {'evidence', 'academic'}
    if not isinstance(data, dict) or (set(data) != expected and not (normalized and set(data) == expected - {'academic'})):
        fail('expected exactly authors, year, journal, title, evidence, academic.')
    if not isinstance(data['authors'], list) or any(not isinstance(a, str) or not a.strip() for a in data['authors']):
        fail('authors must be an array of nonempty strings (or []).')
    if any(not isinstance(data[key], str) for key in ('year', 'journal', 'title')):
        fail('year, journal and title must be strings.')
    year = data['year']
    if year and not re.fullmatch(r'[1-9]\d{3}', year) and not (normalized and year == 'n.d.'):
        fail('year must be empty or four digits.')
    evidence = data['evidence']
    if not isinstance(evidence, dict) or set(evidence) != set(FIELDS):
        fail('evidence must contain exactly authors, year, journal, title.')
    for field, citations in evidence.items():
        if not isinstance(citations, list):
            fail(f'evidence.{field} must be an array.')
        for item in citations:
            if not isinstance(item, dict) or set(item) != {'page', 'quote'}:
                fail(f'evidence.{field} must contain page/quote objects.')
            if type(item['page']) is not int or item['page'] < 1 or not page_count or item['page'] > page_count:
                fail('evidence page is outside the supplied PDF.')
            if not isinstance(item['quote'], str) or not 1 <= len(item['quote'].strip()) <= 500:
                fail('evidence quote must contain 1–500 characters.')
    if 'academic' in data:
        validate_academic(data['academic'], data['authors'], page_count)
    return data


def validate_academic(academic, authors, page_count):
    def fail(message):
        raise ValueError('Invalid academic schema: ' + message)
    if not isinstance(academic, dict) or set(academic) != {'author_details', 'document_kind', 'dates'}:
        fail('expected author_details, document_kind and dates.')
    if academic['document_kind'] not in DOCUMENT_KINDS:
        fail('unknown document kind.')
    details = academic['author_details']
    if not isinstance(details, list) or len(details) != len(authors):
        fail('author details must align with authors.')
    for name, detail in zip(authors, details):
        if not isinstance(detail, dict) or set(detail) != {'literal', 'kind', 'given', 'family', 'suffix'}:
            fail('invalid author fields.')
        if any(not isinstance(value, str) for value in detail.values()) or detail['kind'] not in AUTHOR_KINDS:
            fail('invalid author types.')
        if compact(detail['literal']) != compact(name):
            fail('author literal does not match authors.')
        if detail['kind'] != 'person' and any(detail[key] for key in ('given', 'family', 'suffix')):
            fail('organization/unknown authors must use a literal without personal parts.')
    if not isinstance(academic['dates'], list):
        fail('dates must be an array.')
    for date in academic['dates']:
        if not isinstance(date, dict) or set(date) != {'kind', 'year', 'page', 'quote'}:
            fail('invalid date fields.')
        if date['kind'] not in DATE_KINDS or not isinstance(date['year'], str) or not re.fullmatch(r'[1-9]\d{3}', date['year']):
            fail('invalid date kind or year.')
        if type(date['page']) is not int or not page_count or not 1 <= date['page'] <= page_count:
            fail('date page outside the supplied PDF.')
        if not isinstance(date['quote'], str) or not 1 <= len(date['quote'].strip()) <= 500:
            fail('invalid date quote.')
        if not re.search(r'(?<!\d)' + date['year'] + r'(?!\d)', date['quote']):
            fail('date year must appear in its quoted passage.')


def parse_metadata(text, page_count):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Invalid metadata schema: duplicate key ' + key)
            result[key] = value
        return result
    try:
        data = json.loads(text, object_pairs_hook=unique_object)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError('Invalid JSON metadata response.') from exc
    return validate_metadata(data, page_count)
