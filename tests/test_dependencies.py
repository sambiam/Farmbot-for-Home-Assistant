"""Dependency-contract regression tests (Part 1 of the Vision setup fix).

Root cause guarded here: Home Assistant Core provides and pins Pillow
(Pillow==12.2.0 in Core's package_constraints.txt for 2026.7.x). A custom
integration that *also* declares a different Pillow version (the old
manifest pinned Pillow==12.3.0) makes Core's requirements installer resolve
the manifest requirements against that constraint file, the pin conflicts,
and setup fails with:

    Requirements for farmbot not found: ['Pillow==12.3.0']

The fix is to not declare Pillow in the manifest at all and rely on the
Core-provided version. These tests fail if that regresses.
"""
import json
from pathlib import Path

import pytest

MANIFEST = Path(__file__).resolve().parents[1] / "custom_components" / "farmbot" / "manifest.json"


def _requirements():
    data = json.loads(MANIFEST.read_text())
    return data["requirements"]


def test_manifest_does_not_declare_pillow():
    """Pillow is Core-provided; re-declaring it conflicts with Core's constraint."""
    reqs = _requirements()
    lowered = [r.lower() for r in reqs]
    assert not any(r.startswith("pillow") for r in lowered), (
        f"manifest must not declare Pillow (Core provides it); got {reqs}"
    )


def test_manifest_keeps_non_core_requirements():
    reqs = _requirements()
    joined = " ".join(reqs).lower()
    assert "requests==" in joined
    assert "paho-mqtt==" in joined


def test_manifest_version_is_semver():
    data = json.loads(MANIFEST.read_text())
    parts = data["version"].split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts), data["version"]


def test_pil_is_importable_from_core_provided_pillow():
    """Part 8/3: `from PIL import Image, ImageOps` must succeed in the target env."""
    pytest.importorskip("PIL")
    from PIL import Image, ImageOps

    assert hasattr(Image, "open")
    assert hasattr(ImageOps, "exif_transpose")
