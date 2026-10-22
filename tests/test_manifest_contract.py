"""Keep Home Assistant's own dependencies out of the integration manifest."""
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ManifestContract(unittest.TestCase):
    """Protect the dependency boundary enforced by Hassfest."""

    def test_aiodns_is_supplied_by_home_assistant(self):
        """HA 2025.12 already supplies aiodns; custom manifests must not repeat it."""
        manifest = json.loads(
            (ROOT / 'custom_components/cloudweatherproxy/manifest.json').read_text()
        )
        self.assertEqual(manifest.get('requirements', []), [])
        self.assertEqual(manifest['dependencies'], ['http'])
        self.assertEqual(manifest['version'], '2025.12.0')


if __name__ == '__main__':
    unittest.main()
