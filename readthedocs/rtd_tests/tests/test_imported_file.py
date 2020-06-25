import os
from unittest import mock

from django.conf import settings
from django.core.files.storage import get_storage_class
from django.test import TestCase

from readthedocs.config.config import BuildConfigV2
from readthedocs.projects.models import HTMLFile, ImportedFile, Project
from readthedocs.projects.tasks import (
    _create_imported_files,
    _create_intersphinx_data,
    _sync_imported_files,
)
from readthedocs.sphinx_domains.models import SphinxDomain

base_dir = os.path.dirname(os.path.dirname(__file__))


class ImportedFileTests(TestCase):
    fixtures = ['eric', 'test_data']

    storage = get_storage_class(settings.RTD_BUILD_MEDIA_STORAGE)()

    def setUp(self):
        self.project = Project.objects.get(slug='pip')
        self.version = self.project.versions.first()

        self.test_dir = os.path.join(base_dir, 'files')
        self._copy_storage_dir()

    def _manage_imported_files(self, version, commit, build, config=None):
        """Helper function for the tests to create and sync ImportedFiles."""
        if not config:
            config = BuildConfigV2({}, {}, 'readthedocs.yaml')
            config.validate()
        _create_imported_files(
            version=version,
            commit=commit,
            build=build,
            config=config,
        )
        _sync_imported_files(version, build, set())

    def _copy_storage_dir(self):
        """Copy the test directory (rtd_tests/files) to storage"""
        self.storage.copy_directory(
            self.test_dir,
            self.project.get_storage_path(
                type_='html',
                version_slug=self.version.slug,
                include_file=False,
            ),
        )

    def test_properly_created(self):
        # Only 2 files in the directory is HTML (test.html, api/index.html)
        self.assertEqual(ImportedFile.objects.count(), 0)
        self._manage_imported_files(self.version, 'commit01', 1)
        self.assertEqual(ImportedFile.objects.count(), 2)
        self._manage_imported_files(self.version, 'commit01', 2)
        self.assertEqual(ImportedFile.objects.count(), 2)

        self.project.cdn_enabled = True
        self.project.save()

        # CDN enabled projects => save all files
        self._manage_imported_files(self.version, 'commit01', 3)
        self.assertEqual(ImportedFile.objects.count(), 4)

    def test_update_commit(self):
        self.assertEqual(ImportedFile.objects.count(), 0)
        self._manage_imported_files(self.version, 'commit01', 1)
        self.assertEqual(ImportedFile.objects.first().commit, 'commit01')
        self._manage_imported_files(self.version, 'commit02', 2)
        self.assertEqual(ImportedFile.objects.first().commit, 'commit02')

    def test_page_default_rank(self):
        ranking = {}
        config = BuildConfigV2({}, {'search': {'ranking': ranking}}, 'readthedocs.yaml')
        config.validate()

        self.assertEqual(HTMLFile.objects.count(), 0)
        self._manage_imported_files(self.version, 'commit01', 1, config)

        self.assertEqual(HTMLFile.objects.count(), 2)
        self.assertEqual(HTMLFile.objects.filter(rank=1).count(), 2)

    def test_page_custom_rank_glob(self):
        ranking = {
            '*index.html': 5,
        }
        config = BuildConfigV2({}, {'search': {'ranking': ranking}}, 'readthedocs.yaml')
        config.validate()
        self._manage_imported_files(self.version, 'commit01', 1, config)

        self.assertEqual(HTMLFile.objects.count(), 2)
        file_api = HTMLFile.objects.get(path='api/index.html')
        file_test = HTMLFile.objects.get(path='test.html')
        self.assertEqual(file_api.rank, 5)
        self.assertEqual(file_test.rank, 1)

    def test_page_custom_rank_several(self):
        ranking = {
            'test.html': 5,
            'api/index.html': 2,
        }
        config = BuildConfigV2({}, {'search': {'ranking': ranking}}, 'readthedocs.yaml')
        config.validate()
        self._manage_imported_files(self.version, 'commit01', 1, config)

        self.assertEqual(HTMLFile.objects.count(), 2)
        file_api = HTMLFile.objects.get(path='api/index.html')
        file_test = HTMLFile.objects.get(path='test.html')
        self.assertEqual(file_api.rank, 2)
        self.assertEqual(file_test.rank, 5)

    def test_page_custom_rank_precedence(self):
        ranking = {
            '*.html': 5,
            'api/index.html': 2,
        }
        config = BuildConfigV2({}, {'search': {'ranking': ranking}}, 'readthedocs.yaml')
        config.validate()
        self._manage_imported_files(self.version, 'commit01', 1, config)

        self.assertEqual(HTMLFile.objects.count(), 2)
        file_api = HTMLFile.objects.get(path='api/index.html')
        file_test = HTMLFile.objects.get(path='test.html')
        self.assertEqual(file_api.rank, 2)
        self.assertEqual(file_test.rank, 5)

    def test_page_custom_rank_precedence_inverted(self):
        ranking = {
            'api/index.html': 2,
            '*.html': 5,
        }
        config = BuildConfigV2({}, {'search': {'ranking': ranking}}, 'readthedocs.yaml')
        config.validate()
        self._manage_imported_files(self.version, 'commit01', 1, config)

        self.assertEqual(HTMLFile.objects.count(), 2)
        file_api = HTMLFile.objects.get(path='api/index.html')
        file_test = HTMLFile.objects.get(path='test.html')
        self.assertEqual(file_api.rank, 5)
        self.assertEqual(file_test.rank, 5)

    def test_update_content(self):
        test_dir = os.path.join(base_dir, 'files')
        self.assertEqual(ImportedFile.objects.count(), 0)

        with open(os.path.join(test_dir, 'test.html'), 'w+') as f:
            f.write('Woo')

        self._copy_storage_dir()

        self._manage_imported_files(self.version, 'commit01', 1)
        self.assertEqual(ImportedFile.objects.get(name='test.html').md5, 'c7532f22a052d716f7b2310fb52ad981')
        self.assertEqual(ImportedFile.objects.count(), 2)

        with open(os.path.join(test_dir, 'test.html'), 'w+') as f:
            f.write('Something Else')

        self._copy_storage_dir()

        self._manage_imported_files(self.version, 'commit02', 2)
        self.assertNotEqual(ImportedFile.objects.get(name='test.html').md5, 'c7532f22a052d716f7b2310fb52ad981')
        self.assertEqual(ImportedFile.objects.count(), 2)

    @mock.patch('readthedocs.projects.tasks.os.path.exists')
    def test_create_intersphinx_data(self, mock_exists):
        mock_exists.return_Value = True

        # Test data for objects.inv file
        test_objects_inv = {
            'cpp:function': {
                'sphinx.test.function': [
                    'dummy-proj-1',
                    'dummy-version-1',
                    'test.html#epub-faq',  # file generated by ``sphinx.builders.html.StandaloneHTMLBuilder``
                    'dummy-func-name-1',
                ]
            },
            'py:function': {
                'sample.test.function': [
                    'dummy-proj-2',
                    'dummy-version-2',
                    'test.html#sample-test-func',  # file generated by ``sphinx.builders.html.StandaloneHTMLBuilder``
                    'dummy-func-name-2'
                ]
            },
            'js:function': {
                'testFunction': [
                    'dummy-proj-3',
                    'dummy-version-3',
                    'api/#test-func',  # file generated by ``sphinx.builders.dirhtml.DirectoryHTMLBuilder``
                    'dummy-func-name-3'
                ]
            }
        }

        with mock.patch(
            'sphinx.ext.intersphinx.fetch_inventory',
            return_value=test_objects_inv
        ) as mock_fetch_inventory:

            config = BuildConfigV2({}, {}, 'readthedocs.yaml')
            config.validate()
            _create_imported_files(
                version=self.version,
                commit='commit01',
                build=1,
                config=config,
            )
            _create_intersphinx_data(self.version, 'commit01', 1)

            # there will be two html files,
            # `api/index.html` and `test.html`
            self.assertEqual(
                HTMLFile.objects.all().count(),
                2
            )
            self.assertEqual(
                HTMLFile.objects.filter(path='test.html').count(),
                1
            )
            self.assertEqual(
                HTMLFile.objects.filter(path='api/index.html').count(),
                1
            )

            html_file_api = HTMLFile.objects.filter(path='api/index.html').first()

            self.assertEqual(
                SphinxDomain.objects.all().count(),
                3
            )
            self.assertEqual(
                SphinxDomain.objects.filter(html_file=html_file_api).count(),
                1
            )
