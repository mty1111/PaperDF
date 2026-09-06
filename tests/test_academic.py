"""Academic normalization policy, provider contract, and saved naming settings."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import pdf_metadata_renamer as app
from paperdf_academic import (parse_name, title_style, canonical_journal, parse_aliases,
                              DEFAULT_ALIASES, select_year, academic_review_reasons)
from paperdf_cache import ExtractionCache
from paperdf_schema import FIELDS, parse_metadata
from paperdf_session import BatchStore


def person(literal, given, family, suffix=''):
    return dict(literal=literal, kind='person', given=given, family=family, suffix=suffix)


def date(kind, year, page=1):
    return dict(kind=kind, year=year, page=page, quote=f'{kind} {year}')


def payload():
    return {'authors': ['Gabriel García Márquez', 'World Bank'], 'year': '2020',
            'title': 'GDP and pH in eBay data', 'journal': 'AER',
            'evidence': {field: [] for field in FIELDS},
            'academic': {'document_kind': 'published_article',
                         'author_details': [person('Gabriel García Márquez', 'Gabriel', 'García Márquez'),
                            dict(literal='World Bank', kind='organization', given='', family='', suffix='')],
                         'dates': [date('received', '2020'), date('online', '2023'), date('publication', '2024', 2)]}}


class AcademicPolicyTests(unittest.TestCase):
    def test_particles_comma_forms_hyphens_and_suffixes(self):
        for name, family in [('Ludwig van Beethoven', 'van Beethoven'), ('Luis de la Cruz', 'de la Cruz'),
                             ('van der Waals, Johannes Diderik', 'van der Waals'),
                             ('García Márquez, Gabriel', 'García Márquez'), ('Jean-Paul Sartre', 'Sartre'),
                             ('Martin Luther King, Jr.', 'King'), ('王小明', '王小明')]:
            with self.subTest(name=name):
                self.assertEqual(parse_name(name)['surname'], family)
        self.assertEqual(app.format_authors_list(['Jean-Paul Sartre'], True, '{surname}, {first_initial}.'), 'Sartre, J.-P.')
        self.assertEqual(app.format_authors_list(['Martin Luther King, Jr.'], False, '{surname} {suffix}'), 'King Jr.')

    def test_structured_compound_names_and_organizations_override_person_template(self):
        data = payload()
        name = app.build_new_filename(data, naming_settings={'pattern': '{authors}.pdf', 'author_format': '{surname}'})
        self.assertEqual(name, 'García Márquez, World Bank.pdf')
        detail = person('Charles de la Vallée Poussin', 'Charles', 'de la Vallée Poussin')
        self.assertEqual(app.format_authors_list([detail['literal']], False, '{family}', [detail]), 'de la Vallée Poussin')

    def test_unknown_author_is_kept_whole_and_requires_review(self):
        data = payload()
        data['academic']['author_details'][0].update(kind='unknown', given='', family='', suffix='')
        self.assertIn('Gabriel García Márquez', app.build_new_filename(data))
        self.assertIn('Author type', ' '.join(academic_review_reasons(data)))

    def test_default_capitalization_preserves_acronyms_mixed_case_and_math(self):
        title = r'GDP and DSGE: pH, eBay, LaTeX and $\beta$'
        self.assertEqual(title_style(title), title)
        self.assertEqual(title_style('  中文标题\n与 DNA  '), '中文标题 与 DNA')
        self.assertEqual(title_style(title, 'title'), r'GDP and DSGE: pH, eBay, LaTeX and $\beta$')
        self.assertEqual(title_style('GDP AND DSGE MODELS', 'title'), 'GDP and DSGE Models')

    def test_aliases_are_exact_configurable_and_never_applied_to_publishers(self):
        self.assertEqual(canonical_journal('a.e.r.', DEFAULT_ALIASES), 'American Economic Review')
        self.assertEqual(canonical_journal('JPE', DEFAULT_ALIASES), 'Journal of Political Economy')
        self.assertEqual(canonical_journal('QJE', DEFAULT_ALIASES), 'The Quarterly Journal of Economics')
        self.assertEqual(canonical_journal('AER: Insights', DEFAULT_ALIASES), 'AER: Insights')
        self.assertEqual(canonical_journal('AER', DEFAULT_ALIASES, True), 'AER')
        self.assertEqual(canonical_journal('XYZ', 'XYZ = 自定义期刊'), '自定义期刊')
        self.assertEqual(canonical_journal('AER', ''), 'AER')
        with self.assertRaisesRegex(ValueError, 'Conflicting'):
            parse_aliases('AER = First\na.e.r. = Second')

    def test_published_date_policy_ignores_received_accepted_and_accessed(self):
        academic = payload()['academic']
        academic['dates'] += [date('accepted', '2022'), date('accessed', '2026'), date('revision', '2025')]
        self.assertEqual(select_year(academic)[0], '2024')
        academic['dates'] = [item for item in academic['dates'] if item['kind'] != 'publication']
        self.assertEqual(select_year(academic)[0], '2023')

    def test_preprint_uses_latest_revision_and_book_uses_present_edition(self):
        self.assertEqual(select_year({'document_kind': 'preprint', 'dates': [date('preprint', '2020'),
            date('revision', '2021'), date('revision', '2023'), date('accessed', '2026')]})[0], '2023')
        self.assertEqual(select_year({'document_kind': 'book', 'dates': [date('original', '1970'),
            date('copyright', '2022'), date('publication', '2023')]})[0], '2023')

    def test_ambiguous_or_unsupported_dates_are_held_for_review(self):
        for academic in ({'document_kind': 'published_article', 'dates': [date('publication', '2023'), date('publication', '2024')]},
                         {'document_kind': 'book', 'dates': [date('copyright', '2023')]},
                         {'document_kind': 'unknown', 'dates': [date('publication', '2023')]}):
            self.assertEqual(select_year(academic)[0], '')

    def test_extended_schema_rejects_misalignment_invented_date_and_person_parts_for_org(self):
        data = payload()
        parse_metadata(json.dumps(data), 2)
        variants = []
        changed = deepcopy(data); changed['academic']['author_details'][0]['literal'] = 'Someone else'; variants.append(changed)
        changed = deepcopy(data); changed['academic']['author_details'][1]['family'] = 'Bank'; variants.append(changed)
        changed = deepcopy(data); changed['academic']['dates'][0]['quote'] = 'No year here'; variants.append(changed)
        changed = deepcopy(data); changed['academic']['dates'][0]['page'] = 3; variants.append(changed)
        for changed in variants:
            with self.assertRaisesRegex(ValueError, 'academic schema'):
                parse_metadata(json.dumps(changed), 2)

    def test_real_extraction_selects_date_and_cache_preserves_academic_details(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'input.pdf'
            writer = app.PdfWriter()
            for _ in range(2):
                writer.add_blank_page(width=100, height=200)
            with path.open('wb') as stream:
                writer.write(stream)
            sdk = Mock()
            sdk.models.generate_content.return_value.text = json.dumps(payload())
            cache = ExtractionCache(Path(directory) / 'cache.sqlite3')
            def extract():
                return app.prepare_preview([str(path)], 2, False, sdk, 'fake', lambda: False,
                                           lambda *args: None, cache=cache)[0]
            first, second = extract(), extract()
            self.assertEqual(first['status'], 'Extracted')
            self.assertEqual(first['metadata']['year'], '2024')
            self.assertEqual(first['metadata']['title'], payload()['title'])
            self.assertEqual(first['metadata']['academic'], payload()['academic'])
            self.assertEqual(second['extraction_source'], 'cache')
            self.assertEqual(second['metadata'], first['metadata'])
            self.assertEqual(sdk.files.upload.call_count, 1)
            self.assertIn('García Márquez, World Bank', app.build_new_filename(second['metadata']))
            naming = {'pattern': '{journal} - {title}.pdf', 'author_format': '{surname}', 'unpublished': 'Unknown',
                      'title_style': 'title', 'journal_aliases': 'AER = Local Journal'}
            store = BatchStore(Path(directory) / 'batch-state')
            store.start([str(path)], {'pages': 2, 'is_book': False, 'naming': naming})
            restored = BatchStore(store.directory).load()['context']['naming']
            self.assertEqual(restored, naming)
            self.assertTrue(app.build_new_filename(second['metadata'], naming_settings=restored).startswith('Local Journal - GDP'))

    def test_conflicting_dates_and_unknown_names_hold_automatic_processing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'input.pdf'
            writer = app.PdfWriter()
            for _ in range(2):
                writer.add_blank_page(width=100, height=200)
            with path.open('wb') as stream:
                writer.write(stream)
            sdk = Mock()
            conflict = payload()
            conflict['academic']['dates'].append(date('publication', '2025'))
            unknown = payload()
            unknown['academic']['author_details'][0].update(kind='unknown', given='', family='', suffix='')
            for variant in (conflict, unknown):
                sdk.models.generate_content.return_value.text = json.dumps(variant)
                row = app.prepare_preview([str(path)], 2, False, sdk, 'fake', lambda: False, lambda *args: None)[0]
                self.assertEqual(row['status'], 'Needs review')
                self.assertFalse(row['selected'])
                self.assertNotIn('resume_action', row)
                self.assertTrue(path.exists())


if __name__ == '__main__':
    unittest.main()
