"""Browser imports must remain reachable through the explicit static allowlist."""
from pathlib import Path
import posixpath
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from oms_client.api.routes.workspace import STATIC_FILES
from oms_client.paths import FRONTEND


class BrowserAssetTests(unittest.TestCase):
    def test_relative_imports_of_served_modules_are_served(self):
        for name in STATIC_FILES:
            if not name.endswith('.js'):
                continue
            source = (Path(FRONTEND) / name).read_text()
            for dependency in re.findall(r"(?:from\s*|import\s*)['\"]([^'\"]+)['\"]", source):
                if dependency.startswith('.'):
                    target = posixpath.normpath(posixpath.join(posixpath.dirname(name), dependency))
                    with self.subTest(module=name, dependency=dependency):
                        self.assertIn(target, STATIC_FILES)
                        self.assertTrue((Path(FRONTEND) / target).is_file())


if __name__ == '__main__':
    unittest.main()
