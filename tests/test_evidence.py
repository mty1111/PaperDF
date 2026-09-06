"""Offline checks for source evidence and the exact multi-page review snapshot."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

_config = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {'LOCALAPPDATA': _config.name, 'GEMINI_API_KEY': ''}):
    import pdf_metadata_renamer as app


def make_pdf(page_count):
    writer = app.PdfWriter()
    for index in range(page_count):
        writer.add_blank_page(width=100 + index, height=200)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


class EvidenceExtractionTests(unittest.TestCase):
    def extract(self, payload, *, pages=4, is_book=False):
        client = Mock()
        client.files.upload.return_value.name = 'files/evidence'
        client.models.generate_content.return_value.text = json.dumps(payload)
        metadata = app.get_metadata_from_snippet(make_pdf(pages), is_book, client, 'test-model')
        client.files.delete.assert_called_once_with(name='files/evidence')
        return metadata, client

    def test_later_physical_pages_have_schema_bounds_and_original_quotes(self):
        payload = {
            'authors': ['Ada Lovelace'], 'year': '1843',
            'journal': '', 'title': 'Notes',
            'evidence': {
                'authors': [{'page': 2, 'quote': 'ADA LOVELACE'}],
                'year': [{'page': 4, 'quote': 'Published 1843'}],
                'title': [{'page': 3, 'quote': 'NOTES\non the Engine'}],
                'journal': [],
            },
        }
        metadata, client = self.extract(payload)
        self.assertEqual(metadata['evidence'], payload['evidence'])
        config = client.models.generate_content.call_args.kwargs['config']
        schema = config.response_json_schema
        self.assertEqual(schema['properties']['evidence']['properties']['title']['items']['properties']['page']['maximum'], 4)
        self.assertEqual(set(schema['required']), {'authors', 'year', 'journal', 'title', 'evidence'})
        supported_keywords = {
            'type', 'properties', 'required', 'additionalProperties',
            'items', 'minimum', 'maximum', 'description',
        }

        def assert_supported(node):
            self.assertLessEqual(set(node), supported_keywords)
            for child in node.get('properties', {}).values():
                assert_supported(child)
            if isinstance(node.get('items'), dict):
                assert_supported(node['items'])

        assert_supported(schema)
        quote_schema = schema['properties']['evidence']['properties']['title']['items']['properties']['quote']
        self.assertEqual(quote_schema, {'type': 'string', 'description': 'Exact visible text, 1-500 characters'})
        self.assertIn('physical page', config.system_instruction)
        self.assertIn('ignoring printed page numbers', config.system_instruction)

    def test_non_schema_shapes_are_rejected_and_uploads_deleted(self):
        base = {'authors': ['Ada'], 'year': '1843', 'journal': '', 'title': 'Notes',
                'evidence': {key: [] for key in ('authors', 'year', 'journal', 'title')}}
        invalid = [dict(base, authors='Ada'), dict(base, year=1843), dict(base, year='1843/1844'),
                   dict(base, publisher='Press'), {'metadata': base}, [base],
                   {key: value for key, value in base.items() if key != 'evidence'}]
        for payload in invalid:
            with self.subTest(payload=payload):
                sdk = Mock()
                sdk.files.upload.return_value.name = 'files/invalid'
                sdk.models.generate_content.return_value.text = json.dumps(payload)
                with self.assertRaisesRegex(ValueError, 'schema'):
                    app.get_metadata_from_snippet(make_pdf(4), False, sdk, 'fake')
                sdk.files.delete.assert_called_once_with(name='files/invalid')

    def test_invalid_citations_are_rejected_without_coercion(self):
        for citation in ({'page': 0, 'quote': 'zero'}, {'page': 5, 'quote': 'outside'},
                         {'page': True, 'quote': 'boolean'}, {'page': '2', 'quote': 'string'},
                         {'page': 1, 'quote': ' '}, {'page': 1, 'quote': 'x' * 501},
                         {'page': 1, 'quote': 42}, {'page': 1}):
            with self.subTest(citation=citation):
                payload = {'authors': [], 'year': '', 'journal': '', 'title': 'Notes',
                           'evidence': {key: [] for key in ('authors', 'year', 'journal', 'title')}}
                payload['evidence']['title'] = [citation]
                with self.assertRaisesRegex(ValueError, 'schema'):
                    self.extract(payload)

    def test_duplicate_json_keys_are_rejected(self):
        sdk = Mock()
        sdk.models.generate_content.return_value.text = '{"title":"A","title":"B"}'
        with self.assertRaisesRegex(ValueError, 'duplicate key'):
            app.get_metadata_from_snippet(make_pdf(2), False, sdk, 'fake')
        sdk.files.delete.assert_called_once()

    def test_api_failure_also_deletes_uploaded_snippet(self):
        client = Mock()
        client.files.upload.return_value.name = 'files/evidence'
        client.models.generate_content.side_effect = RuntimeError('provider unavailable')
        with self.assertRaisesRegex(RuntimeError, 'provider unavailable'):
            app.get_metadata_from_snippet(make_pdf(3), False, client, 'test-model')
        client.files.delete.assert_called_once_with(name='files/evidence')


class ReviewSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.source = self.directory / 'MiXeD-Name.PDF'
        self.source.write_bytes(make_pdf(7))
        self.review_dir = self.directory / 'review'
        self.metadata = {'authors': ['Ada Lovelace'], 'year': '1843', 'journal': '', 'title': 'Notes', 'evidence': {}}

    def prepare(self, *, metadata=None, pages=4, is_book=False, error=None):
        with patch.object(app, 'get_metadata_from_snippet', return_value=metadata if metadata is not None else self.metadata, side_effect=error) as extract:
            rows = app.prepare_preview(
                [str(self.source)], pages, is_book, object(), 'test-model',
                lambda: False, lambda *args: None, review_dir=self.review_dir,
            )
        return rows[0], extract

    def test_snapshot_contains_exact_sent_pages_and_survives_source_rename(self):
        original_bytes = self.source.read_bytes()
        row, extract = self.prepare()
        snapshot = Path(row['snippet_path'])
        self.assertEqual(snapshot.parent, self.review_dir)
        self.assertEqual(snapshot.read_bytes(), extract.call_args.args[0])
        self.assertEqual(len(app.PdfReader(str(snapshot)).pages), 4)
        self.assertEqual(row['page_count'], 4)
        self.assertEqual(row['original_source'], str(self.source))
        self.assertEqual(row['source'], str(self.source))
        self.assertEqual(row['fingerprint'], app.fingerprint_file(self.source))
        self.assertEqual(self.source.read_bytes(), original_bytes)
        self.source.rename(self.directory / 'renamed.pdf')
        self.assertTrue(snapshot.exists())
        self.assertNotIn('snippet_bytes', row)
        self.assertTrue(row['selected'])
        self.assertFalse(row['needs_review'])

    def test_actual_page_count_is_used_when_requested_range_exceeds_source(self):
        row, _ = self.prepare(pages=12, is_book=True)
        self.assertEqual(row['page_count'], 7)
        self.assertTrue(row['is_book'])
        self.assertEqual(len(app.PdfReader(row['snippet_path']).pages), 7)

    def test_provider_error_retains_snapshot_for_manual_correction(self):
        row, extract = self.prepare(error=RuntimeError('Quota exceeded'))
        self.assertEqual(Path(row['snippet_path']).read_bytes(), extract.call_args.args[0])
        self.assertEqual(row['status'], 'Error')
        self.assertFalse(row['selected'])
        self.assertTrue(row['needs_review'])
        self.assertEqual(row['extraction_error'], 'Quota exceeded')
        self.assertIn('Quota exceeded', row['reason'])

    def test_missing_required_fields_are_held_while_journal_and_evidence_are_optional(self):
        for field, value in [('title', ''), ('authors', []), ('year', 'n.d.'), ('year', '1843/1844'), ('year', '0000')]:
            with self.subTest(field=field, value=value):
                metadata = dict(self.metadata, **{field: value})
                row, _ = self.prepare(metadata=metadata)
                self.assertFalse(row['selected'])
                self.assertTrue(row['needs_review'])
                self.assertEqual(row['status'], 'Needs review')
                self.assertIn(field, row['reason'])
        row, _ = self.prepare()
        self.assertTrue(row['selected'])
        self.assertFalse(row['needs_review'])
        self.assertEqual(row['status'], 'Extracted')
        self.assertEqual(row['reason'], '')

    def test_every_run_uses_a_distinct_snapshot_and_fingerprint_precedes_extraction(self):
        original_fingerprint = app.fingerprint_file(self.source)

        def change_source(*args):
            self.source.write_bytes(make_pdf(1))
            return self.metadata

        with patch.object(app, 'get_metadata_from_snippet', side_effect=change_source):
            first = app.prepare_preview(
                [str(self.source)], 4, False, object(), 'test-model',
                lambda: False, lambda *args: None, review_dir=self.review_dir,
            )[0]
        self.assertEqual(first['fingerprint'], original_fingerprint)
        self.assertNotEqual(first['fingerprint'], app.fingerprint_file(self.source))
        second, _ = self.prepare()
        self.assertNotEqual(first['snippet_path'], second['snippet_path'])
        self.assertEqual(len(app.PdfReader(first['snippet_path']).pages), 4)
        self.assertEqual(len(app.PdfReader(second['snippet_path']).pages), 1)


if __name__ == '__main__':
    unittest.main()
