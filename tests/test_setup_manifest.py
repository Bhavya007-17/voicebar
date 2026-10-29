"""A checkout's parent name must not hide actual model files."""

from scripts.setup import file_manifest


def test_model_manifest_excludes_only_cache_inside_model_directory(tmp_path):
    model = tmp_path / ".cache" / "fresh-clone" / "models" / "speech"
    model.mkdir(parents=True)
    (model / "weights.bin").write_bytes(b"owned model fixture")
    (model / ".cache").mkdir()
    (model / ".cache/download.json").write_text("ignored")
    entries = file_manifest(model)
    assert [row["path"] for row in entries] == ["weights.bin"]
    assert entries[0]["size"] == len(b"owned model fixture")
    assert len(entries[0]["sha256"]) == 64
