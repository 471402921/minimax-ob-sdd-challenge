"""Offline contract tests for the wheel-matching prewarm builder. No Docker, no model."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('prewarm_wheels', ROOT / 'prewarm_wheels.py')
prewarm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prewarm)


def make_wheel(directory: Path, name: str, version: str) -> Path:
    path = directory / f'{name.replace("-", "_")}-{version}-py3-none-any.whl'
    info = f'{name.replace("-", "_")}-{version}.dist-info'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr(f'{info}/METADATA', f'Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n')
        archive.writestr(f'{info}/WHEEL', 'Wheel-Version: 1.0\n')
    return path


class WheelSetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.wheels = self.root/'wheels'
        self.wheels.mkdir()
        make_wheel(self.wheels, 'powercontext', '1.4.0')
        make_wheel(self.wheels, 'pylibseekdb', '1.4.0.post2')
        self.context = self.root/'context'

    def tearDown(self):
        self.temp.cleanup()

    def test_prepare_hashes_and_service_requirement(self):
        out = prewarm.prepare(self.context, self.wheels)
        self.assertEqual(out, self.context)
        manifest = json.loads((out/'inputs.json').read_text())
        staged = out/'wheels/player/powercontext-1.4.0-py3-none-any.whl'
        self.assertEqual(manifest['files']['wheels/player/' + staged.name]['sha256'],
                         hashlib.sha256(staged.read_bytes()).hexdigest())
        self.assertEqual((out/'wheels/player/requirements.service.txt').read_text(),
                         'powercontext[cli,server,seekdb]==1.4.0\n')
        self.assertTrue((out/'Dockerfile').is_file())
        self.assertNotIn(str(self.root), json.dumps(manifest))

    def test_refuses_wrong_powercontext_version(self):
        for item in self.wheels.glob('powercontext-*.whl'):
            item.unlink()
        make_wheel(self.wheels, 'powercontext', '1.2.0')
        with self.assertRaises(prewarm.PrewarmError):
            prewarm.prepare(self.context, self.wheels)
        self.assertFalse(self.context.exists())

    def test_refuses_symlink_sdist_and_empty(self):
        (self.wheels/'evil.whl').symlink_to(self.wheels/'powercontext-1.4.0-py3-none-any.whl')
        with self.assertRaises(prewarm.PrewarmError):
            prewarm.prepare(self.context, self.wheels)
        (self.wheels/'evil.whl').unlink()
        (self.wheels/'source.tar.gz').write_text('x')
        with self.assertRaises(prewarm.PrewarmError):
            prewarm.prepare(self.context, self.wheels)
        (self.wheels/'source.tar.gz').unlink()
        with self.assertRaises(prewarm.PrewarmError):
            prewarm.prepare(self.context, self.root/'absent')

    def test_never_overwrites_existing_context(self):
        self.context.mkdir()
        (self.context/'keep').write_text('evidence')
        with self.assertRaises(prewarm.PrewarmError):
            prewarm.prepare(self.context, self.wheels)
        self.assertEqual((self.context/'keep').read_text(), 'evidence')

    def test_rejects_unsafe_tag_and_missing_context(self):
        with self.assertRaises(prewarm.PrewarmError):
            prewarm.build_command(self.context, 'bad tag')
        with self.assertRaises(prewarm.PrewarmError):
            prewarm.build_command(self.context, 'harbor/review:local')

    def test_dry_run_commands_are_inspectable(self):
        prewarm.prepare(self.context, self.wheels)
        plan = prewarm.build(self.context, 'harbor/review:local', dry_run=True)
        self.assertEqual(plan['command'][0], 'docker')
        self.assertFalse(plan['built'])
        verify = prewarm.verify(dry_run=True)['command']
        self.assertIn('none', verify)
        self.assertTrue(any('requirements.service.txt' in part for part in verify))

    def test_verify_mounts_submission_requirements(self):
        req = self.root/'requirements.txt'
        req.write_text('powercontext[cli,server,seekdb]==1.4.0\n')
        command = prewarm.verify_command(req)
        self.assertIn(f'{req.resolve()}:/req.txt:ro', command)
        joined = ' '.join(command)
        self.assertIn('-r /req.txt', joined)
        self.assertIn('/opt/wheels/player/requirements.service.txt', ' '.join(prewarm.verify_command(None)))

    def test_source_images_exist_and_are_hashed(self):
        for name in prewarm.SOURCES:
            self.assertTrue((prewarm.IMAGE_DIR/name).is_file() or (prewarm.SCRIPT_DIR/name).is_file(), name)
        dockerfile = (prewarm.IMAGE_DIR/'Dockerfile').read_text()
        self.assertNotIn('COPY submission', dockerfile)
        self.assertNotIn('/submission', dockerfile)
        self.assertIn('/opt/wheels/player', dockerfile)

    def test_wheel_metadata_is_read_without_extraction(self):
        name, version = prewarm.wheel_metadata(self.wheels/'powercontext-1.4.0-py3-none-any.whl')
        self.assertEqual((name, version), ('powercontext', '1.4.0'))
        bad = self.wheels/'broken.whl'
        bad.write_text('not a zip')
        with self.assertRaises(prewarm.PrewarmError):
            prewarm.wheel_metadata(bad)


if __name__ == '__main__':
    unittest.main()
