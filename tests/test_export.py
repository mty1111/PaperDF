"""CSV reports retain Unicode, current paths, and complete batch results."""
import copy
import csv
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paperdf_export import COLUMNS, export_results


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.target = self.folder / 'results.csv'

    def read(self):
        with self.target.open(encoding='utf-8-sig', newline='') as stream:
            return list(csv.DictReader(stream))

    def test_report_roundtrips_unicode_quotes_newlines_and_current_paths(self):
        source = self.folder / '乱码.pdf'
        source.write_bytes(b'unchanged PDF')
        rows = [{'original_source': str(source), 'source': str(self.folder / '新标题.pdf'),
                 'metadata': {'title': '标题, "quoted"\n第二行', 'authors': ['马天宇', 'Ada Lovelace'],
                              'year': '2026', 'journal': '期刊'},
                 'status': 'Renamed', 'needs_review': False, 'requested_pages': 8, 'page_count': 6},
                {'source': str(self.folder / 'pending.pdf'), 'status': 'Error', 'needs_review': True,
                 'extraction_error': 'Offline timeout\nTry again'}]
        before = copy.deepcopy(rows)
        self.assertEqual(export_results(rows, self.target), 2)
        self.assertTrue(self.target.read_bytes().startswith(b'\xef\xbb\xbf'))
        first, second = self.read()
        self.assertEqual(tuple(first), COLUMNS)
        self.assertEqual(first['title'], rows[0]['metadata']['title'])
        self.assertEqual(first['authors'], '马天宇\nAda Lovelace')
        self.assertEqual(first['original_path'], str(source))
        self.assertEqual(first['current_path'], rows[0]['source'])
        self.assertEqual((first['requested_pages'], first['analyzed_pages']), ('8', '6'))
        self.assertEqual((first['needs_attention'], second['needs_attention']), ('no', 'yes'))
        self.assertEqual(second['original_path'], second['current_path'])
        self.assertEqual(second['requested_pages'], '')
        self.assertEqual(second['details'], 'Offline timeout\nTry again')
        self.assertEqual(rows, before)
        self.assertEqual(source.read_bytes(), b'unchanged PDF')

    def test_spreadsheet_formula_prefixes_are_exported_as_text(self):
        titles = ['=1+1', '+SUM(1,2)', '-2+3', '@SUM(1,2)', '  =1+1', '\tdata', '\rdata', '\ndata']
        export_results([{'metadata': {'title': value}} for value in titles], self.target)
        self.assertEqual([row['title'] for row in self.read()], ["'" + value for value in titles])

    def test_failed_write_keeps_existing_report_and_removes_temporary_file(self):
        self.target.write_bytes(b'previous report')
        with patch('paperdf_export.os.replace', side_effect=PermissionError('report open elsewhere')):
            with self.assertRaises(PermissionError):
                export_results([{'status': 'Pending'}], self.target)
        self.assertEqual(self.target.read_bytes(), b'previous report')
        self.assertEqual(list(self.folder.iterdir()), [self.target])

    def test_report_cannot_overwrite_a_pdf(self):
        source = self.folder / 'original.pdf'
        source.write_bytes(b'keep this PDF')
        with self.assertRaisesRegex(ValueError, '.csv'):
            export_results([], source)
        self.assertEqual(source.read_bytes(), b'keep this PDF')
        self.assertEqual(list(self.folder.iterdir()), [source])

    def test_empty_report_has_headers(self):
        self.assertEqual(export_results([], self.target), 0)
        with self.target.open(encoding='utf-8-sig', newline='') as stream:
            self.assertEqual(list(csv.reader(stream)), [list(COLUMNS)])


if __name__ == '__main__':
    unittest.main()
