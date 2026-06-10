import zipfile

import pytest

from src.io import input_handler


def test_zip_converter_processes_safe_relative_files(tmp_path):
    zip_path = tmp_path / "safe.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("docs/readme.txt", "hello from zip")
        archive.writestr("data/config.json", '{"ok": true}')

    result = input_handler.ZipConverter(str(zip_path))

    assert result.title == "ZIP Archive Contents"
    assert "Total files extracted: 2" in result.text_content
    assert "## File: docs/readme.txt" in result.text_content
    assert "hello from zip" in result.text_content
    assert '"ok": true' in result.text_content


@pytest.mark.parametrize(
    "member_name",
    [
        "../escape.txt",
        "nested/../../escape.txt",
        "/tmp/escape.txt",
        "C:\\temp\\escape.txt",
        "\\\\server\\share\\escape.txt",
    ],
)
def test_zip_converter_rejects_unsafe_member_paths(
    tmp_path,
    monkeypatch,
    member_name,
):
    zip_path = tmp_path / "unsafe.zip"
    extraction_root = tmp_path / "extract"
    extraction_root.mkdir()
    outside_target = tmp_path / "escape.txt"

    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr(member_name, "should not be extracted")

    monkeypatch.setattr(
        input_handler.tempfile,
        "mkdtemp",
        lambda prefix="": str(extraction_root),
    )

    with pytest.raises(ValueError, match="unsafe ZIP member path"):
        input_handler.ZipConverter(str(zip_path))

    assert not outside_target.exists()
