"""Local validation of the provider contract, independent of SDK enforcement."""
import json
import re

FIELDS = ('authors', 'year', 'journal', 'title')


def validate_metadata(data, page_count, *, normalized=False):
    def fail(detail):
        raise ValueError('Invalid metadata schema: ' + detail)
    if not isinstance(data, dict) or set(data) != set(FIELDS) | {'evidence'}:
        fail('expected exactly authors, year, journal, title, evidence.')
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
    return data


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
