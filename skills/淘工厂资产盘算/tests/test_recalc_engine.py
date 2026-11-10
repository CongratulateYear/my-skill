"""Offline engine discovery tests: synthetic bundles, never execute an engine."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from recalc_engine import resolve_recalc_engine
import delivery_enrichment as delivery


class RecalcEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        real_is_file = Path.is_file
        # Synthetic tests must not discover the real macOS host installation.
        files = patch.object(Path, 'is_file', autospec=True, side_effect=lambda p:
                             p.is_relative_to(self.root) and real_is_file(p))
        files.start()
        self.addCleanup(files.stop)
        self.context = dict(environ={}, platform='darwin', home=self.root/'home',
                            executable=self.root/'unrelated/python')

    def file(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return path

    def bundle(self, resources, windows=False):
        return (self.file(resources/'runtime/primary-runtime/dependencies/node/bin'/
                          ('node.exe' if windows else 'node')),
                self.file(resources/'app.asar.unpacked/dsh/node_modules/@deepseek-ai/libreoffice-kit/lib/cli.js'))

    def resolve(self, **kwargs):
        return resolve_recalc_engine(**(self.context | kwargs))

    def test_mac_user_bundle_and_separate_managed_node(self):
        resources = self.root/'home/Applications/DeepSeek Harness.app/Contents/Resources'
        bundled, cli = self.bundle(resources)
        node = self.file(self.root/'home/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/node/bin/node')
        self.assertEqual(self.resolve(), (node, cli))
        node.unlink()
        self.assertEqual(self.resolve(), (bundled, cli))

    def test_mac_system_bundle_without_real_host_dependency(self):
        node, cli = (
            Path('/Applications/DeepSeek Harness.app/Contents/Resources/runtime/primary-runtime/dependencies/node/bin/node'),
            Path('/Applications/DeepSeek Harness.app/Contents/Resources/app.asar.unpacked/dsh/node_modules/@deepseek-ai/libreoffice-kit/lib/cli.js'))
        with patch.object(Path, 'is_file', new=lambda p: p in (node, cli)):
            self.assertEqual(self.resolve(), (node, cli))

    def test_windows_localappdata_programs_and_node_exe(self):
        local = self.root/'LocalAppData'
        expected = self.bundle(local/'Programs/DeepSeek Harness/resources', windows=True)
        self.assertEqual(self.resolve(platform='win32', environ={'LOCALAPPDATA': str(local)}), expected)

    def test_windows_program_files(self):
        programs = self.root/'Program Files'
        expected = self.bundle(programs/'DeepSeek Harness/resources', windows=True)
        self.assertEqual(self.resolve(platform='win32', environ={'ProgramFiles': str(programs)}), expected)

    def test_windows_executable_ancestor_with_resources_node(self):
        install = self.root/'custom/DeepSeek Harness'
        _, cli = self.bundle(install/'resources', windows=True)
        node = self.file(install/'resources/node.exe')
        (install/'resources/runtime/primary-runtime/dependencies/node/bin/node.exe').unlink()
        self.assertEqual(self.resolve(platform='win32', executable=install/'DeepSeek Harness.exe'), (node, cli))

    def test_mac_bundled_python_ancestor(self):
        resources = self.root/'custom/DeepSeek Harness.app/Contents/Resources'
        expected = self.bundle(resources)
        self.assertEqual(self.resolve(executable=resources/'runtime/primary-runtime/dependencies/python/bin/python3'), expected)

    def test_runtime_facts_and_env_resource_hint(self):
        _, cli = self.bundle(self.root/'resources')
        node = self.file(self.root/'managed/node/bin/node')
        self.assertEqual(self.resolve(environ={'DSH_RESOURCES_PATH': str(self.root/'resources')},
                                      runtime_facts={'node': str(node)}), (node, cli))
        self.assertEqual(self.resolve(runtime_facts={'node': str(node),
                         'nodePackages': str(cli.parents[3])}), (node, cli))

    def test_dsh_home_and_runtime_dir_hints(self):
        resources = self.root/'resources'
        bundled, cli = self.bundle(resources)
        bundled.unlink()
        runtime = self.root/'dsh/dsh-runtimes/dsh-primary-runtime'
        node = self.file(runtime/'dependencies/node/bin/node')
        for key, value in (('DSH_HOME', self.root/'dsh'), ('DSH_RUNTIME_DIR', runtime)):
            with self.subTest(key=key):
                self.assertEqual(self.resolve(environ={key: str(value),
                    'DSH_RESOURCES_DIR': str(resources)}), (node, cli))

    def test_explicit_arguments_beat_environment(self):
        node, cli = self.file(self.root/'node'), self.file(self.root/'cli.js')
        self.assertEqual(self.resolve(node=node, cli=cli, environ={
            'TGC_RECALC_NODE': 'invalid', 'TGC_RECALC_CLI': 'invalid'}), (node, cli))

    def test_each_override_authoritative_even_when_bundle_exists(self):
        self.bundle(self.root/'home/Applications/DeepSeek Harness.app/Contents/Resources')
        for key in ('TGC_RECALC_NODE', 'TGC_RECALC_CLI'):
            for invalid in ('', str(self.root/'missing'), str(self.root)):
                with self.subTest(key=key, invalid=invalid):
                    with self.assertRaisesRegex(ValueError, key):
                        self.resolve(environ={key: invalid})
        for key in ('node', 'cli'):
            with self.subTest(argument=key):
                with self.assertRaisesRegex(ValueError, 'override'):
                    self.resolve(**{key: ''})

    def test_partial_override_discovers_only_missing_component(self):
        bundled, cli = self.bundle(self.root/'home/Applications/DeepSeek Harness.app/Contents/Resources')
        node = self.file(self.root/'explicit-node')
        self.assertEqual(self.resolve(environ={'TGC_RECALC_NODE': str(node)}), (node, cli))
        self.assertNotEqual(node, bundled)

    def test_no_installed_engine_fails_closed_no_path_or_process_fallback(self):
        with patch.object(Path, 'is_file', return_value=False), \
             patch('subprocess.run', side_effect=AssertionError('must not execute')):
            with self.assertRaisesRegex(ValueError, 'unavailable.*Node.*LibreOffice Kit'):
                self.resolve(environ={'PATH': '/usr/bin:/opt/homebrew/bin'})

    def test_dependency_directory_hint(self):
        bundled, cli = self.bundle(self.root/'resources')
        bundled.unlink()
        node = self.file(self.root/'dependencies/node/bin/node')
        self.assertEqual(self.resolve(environ={
            'DSH_DEPENDENCIES_DIR': str(self.root/'dependencies'),
            'DSH_RESOURCES_DIR': str(self.root/'resources')}), (node, cli))

    def test_deterministic_first_existing_hint_wins(self):
        first = self.bundle(self.root/'first')
        self.bundle(self.root/'second')
        self.assertEqual(self.resolve(environ={'DSH_RESOURCES_PATH': str(self.root/'first'),
                         'DSH_RESOURCES_DIR': str(self.root/'second')}), first)

    def test_partial_installation_rejected(self):
        node, cli = self.bundle(self.root/'home/Applications/DeepSeek Harness.app/Contents/Resources')
        cli.unlink()
        with self.assertRaisesRegex(ValueError, 'missing installed LibreOffice Kit CLI'):
            self.resolve()
        node.unlink()
        cli.touch()
        with self.assertRaisesRegex(ValueError, 'missing installed Node'):
            self.resolve()

    def test_recalculate_legacy_injection_and_env_override(self):
        node, cli = self.file(self.root/'node'), self.file(self.root/'cli.js')
        with patch.object(delivery, 'NODE', node), patch.object(delivery, 'CLI', cli), \
             patch.object(delivery, 'resolve_recalc_engine', side_effect=ValueError('stop')) as resolver, \
             patch.dict('os.environ', {}, clear=True):
            with self.assertRaisesRegex(ValueError, 'stop'):
                delivery.recalculate('source', 'destination')
            resolver.assert_called_once_with(node=node, cli=cli)
        with patch.object(delivery, 'NODE', node), patch.object(delivery, 'CLI', cli), \
             patch.object(delivery, 'resolve_recalc_engine', side_effect=ValueError('stop')) as resolver, \
             patch.dict('os.environ', {'TGC_RECALC_NODE': 'invalid', 'TGC_RECALC_CLI': 'invalid'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'stop'):
                delivery.recalculate('source', 'destination')
            resolver.assert_called_once_with(node=None, cli=None)


if __name__ == '__main__':
    unittest.main()
