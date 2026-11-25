"""Pytest configuration for the test suite.

This module ensures `custom_components` is available on `sys.path`
so the vendored `cloudweatherproxy` package can be imported when
running `pytest` from the repository root.
"""

import os
import sys

# Ensure `custom_components` is on sys.path so tests can import the vendored package
ROOT = os.path.dirname(__file__)
CUSTOM_COMPONENTS = os.path.join(ROOT, "custom_components")
if CUSTOM_COMPONENTS not in sys.path:
    sys.path.insert(0, CUSTOM_COMPONENTS)


import pytest  # noqa: E402
from homeassistant import loader  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers import frame  # noqa: E402


@pytest.fixture(autouse=True)
def initialize_core_helpers(monkeypatch):
    """Initialize real Core helpers normally installed by bootstrap.

    These small fixtures construct HomeAssistant directly, without booting Core.
    ConfigEntry's real wrappers still require frame reporting and loader caches.
    """
    original = HomeAssistant.__init__

    def initialized(self, *args, **kwargs):
        original(self, *args, **kwargs)
        frame.async_setup(self)
        loader.async_setup(self)
        # Usage reporting needs the actual custom-integration metadata even when
        # these fixtures substitute platform orchestration instead of discovery.
        import json
        from pathlib import Path

        component = Path(ROOT) / "custom_components" / "cloudweatherproxy"
        self.data[loader.DATA_CUSTOM_COMPONENTS] = {
            "cloudweatherproxy": loader.Integration(
                self,
                "custom_components.cloudweatherproxy",
                component,
                json.loads((component / "manifest.json").read_text()),
            )
        }

    monkeypatch.setattr(HomeAssistant, "__init__", initialized)
