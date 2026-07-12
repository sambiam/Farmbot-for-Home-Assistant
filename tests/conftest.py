"""Test setup for isolated FarmBot custom component tests.

Real Home Assistant is not installed here (it is a large, heavy dependency
that is impractical to pin/install just to unit test a small custom
component). Instead ``tests/stubs/homeassistant`` provides a minimal,
hand-written stand-in for exactly the pieces of the ``homeassistant``
package that ``config_flow.py`` and ``manager.py`` import, modelled closely
on real Home Assistant's documented behaviour (see each stub module's
docstring). This lets us exercise the integration's own logic --
unique-ID de-duplication, reauth handling, and MQTT callback behaviour --
without contacting FarmBot or Home Assistant.

See tests/README.md for more detail and the limitations of this approach.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STUBS = Path(__file__).resolve().parent / "stubs"

for path in (str(ROOT), str(STUBS)):
    if path not in sys.path:
        sys.path.insert(0, path)
