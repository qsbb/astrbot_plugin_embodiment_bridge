from __future__ import annotations

import ast
import json
import re
import struct
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VERSION = "1.1.4"


def test_metadata_entrypoint_and_changelog_share_release_version() -> None:
    metadata = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
    metadata_match = re.search(r"^version:\s*([^\s#]+)\s*$", metadata, re.MULTILINE)
    assert metadata_match is not None
    assert metadata_match.group(1) == EXPECTED_VERSION

    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    main_match = re.search(
        r'^__version__\s*=\s*["\']([^"\']+)["\']\s*$',
        main_source,
        re.MULTILINE,
    )
    assert main_match is not None
    assert main_match.group(1) == EXPECTED_VERSION

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## Unreleased\n" in changelog
    assert f"## {EXPECTED_VERSION} - 2026-08-24\n" in changelog
    assert changelog.index("## Unreleased") < changelog.index(
        f"## {EXPECTED_VERSION} - 2026-08-24"
    )
    assert "## 0.1.1 - 2026-08-03" in changelog


def test_page_assets_have_no_stale_version_cache_stamp_and_protocol_stays_1_0() -> None:
    page = (ROOT / "pages" / "pairing" / "index.html").read_text(encoding="utf-8")
    assert './style.css"' in page
    assert './app.js"' in page
    assert re.search(r"[?&](?:v|version)=", page) is None

    manifest = json.loads(
        (ROOT / "fixtures" / "protocol_v1" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["protocol_version"] == "1.0"
    assert EXPECTED_VERSION not in json.dumps(manifest, ensure_ascii=False)


def test_main_uses_public_astrbot_filter_import_path() -> None:
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    imports = [
        node
        for node in ast.walk(ast.parse(main_source))
        if isinstance(node, ast.ImportFrom)
    ]

    assert any(
        node.module == "astrbot.api.event"
        and any(alias.name == "filter" for alias in node.names)
        for node in imports
    )
    assert not any(
        node.module == "astrbot.api"
        and any(alias.name == "filter" for alias in node.names)
        for node in imports
    )


def test_plugin_logo_is_a_valid_square_png() -> None:
    logo = (ROOT / "logo.png").read_bytes()
    assert logo.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(logo) <= 512 * 1024
    width, height = struct.unpack(">II", logo[16:24])
    assert (width, height) == (256, 256)
