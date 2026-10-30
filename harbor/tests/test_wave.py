import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("harbor_wave", ROOT / "harbor" / "wave.py")
wave = importlib.util.module_from_spec(spec)
sys.modules["harbor_wave"] = wave
spec.loader.exec_module(wave)

IMAGE = "sha256:" + "a" * 64


class BaseImageTest(unittest.TestCase):
    def test_accepts_full_sha256(self):
        self.assertEqual(wave.validate_base_image(IMAGE), IMAGE)

    def test_rejects_tags_and_short_ids(self):
        for value in ("harbor/review-wheels:local", "sha256:abc", "sha256:" + "A" * 64, ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                wave.validate_base_image(value)


class ManifestTest(unittest.TestCase):
    def test_failures_are_excluded(self):
        results = [
            {"path": "workdir/harbor/review-1-x", "profile": "light", "task_id": 1},
            {"task_id": 2, "error": "ValueError: no minimax files"},
        ]
        self.assertEqual(wave.manifest_from(results),
                         {"tasks": [{"path": "workdir/harbor/review-1-x", "profile": "light"}]})

    def test_empty_results_give_empty_task_list(self):
        self.assertEqual(wave.manifest_from([]), {"tasks": []})


class LocalImageTest(unittest.TestCase):
    def _run(self, stdout, returncode=0):
        completed = mock.Mock(stdout=stdout, returncode=returncode, stderr="")
        with mock.patch.object(wave.subprocess, "run", return_value=completed) as patched:
            wave.check_local_image(IMAGE)
        return patched

    def test_matching_tag_passes(self):
        patched = self._run(IMAGE + "\n")
        argv = patched.call_args.args[0]
        self.assertEqual(argv[1:3], ["image", "inspect"])
        self.assertEqual(argv[3], "harbor/prewarm:sha-" + "a" * 64)

    def test_missing_or_different_tag_is_refused(self):
        for stdout, returncode in (("", 1), ("sha256:" + "b" * 64, 0)):
            with self.subTest(stdout=stdout, returncode=returncode), \
                    self.assertRaises(ValueError), \
                    mock.patch.object(wave.subprocess, "run",
                                      return_value=mock.Mock(stdout=stdout, returncode=returncode,
                                                             stderr="")):
                wave.check_local_image(IMAGE)


if __name__ == "__main__":
    unittest.main()
