from __future__ import annotations

from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    ".conf",
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".yaml",
    ".yml",
}
SKIP_PARTS = {".git", ".pytest_cache", ".ruff_cache", "__pycache__"}


def test_bridge_sources_do_not_restore_known_personal_defaults() -> None:
    public_contact = "".join(("14839", "04397"))
    forbidden = {
        "".join(("20581", "41897")),
        "".join(("192.168.5", ".88")),
        "".join(("192.168.5", ".70")),
        "".join(("192.168.5", ".71")),
    }
    findings: list[str] = []
    for path in PLUGIN_ROOT.rglob("*"):
        if (
            not path.is_file()
            or path.suffix.lower() not in TEXT_SUFFIXES
            or any(part in SKIP_PARTS for part in path.parts)
            or path == Path(__file__).resolve()
        ):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for value in forbidden:
            if value in text:
                findings.append(f"{path.relative_to(PLUGIN_ROOT)}: forbidden default")
        if public_contact in text and path != PLUGIN_ROOT / "README.md":
            findings.append(f"{path.relative_to(PLUGIN_ROOT)}: personal contact outside README")
    assert findings == []

    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
    assert readme.count(public_contact) == 1
    assert f"QQ：`{public_contact}`" in readme
