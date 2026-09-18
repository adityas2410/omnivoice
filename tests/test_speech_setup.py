import hashlib
import zipfile
from pathlib import Path

import pytest

import omnivoice.speech.setup as setup
from omnivoice.speech.setup import SpeechSetupError


def fixture_assets(tmp_path: Path, *, unsafe: bool = False) -> tuple[bytes, bytes]:
    archive = tmp_path / "fixture.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        name = "../escape.exe" if unsafe else "Release/whisper-cli.exe"
        zipped.writestr(name, b"binary")
        zipped.writestr("Release/ggml-blas.dll", b"runtime")
    return archive.read_bytes(), b"small model"


def configure_hashes(
    monkeypatch: pytest.MonkeyPatch, archive: bytes, model: bytes
) -> None:
    monkeypatch.setattr(setup, "WHISPER_ARCHIVE_SHA256", hashlib.sha256(archive).hexdigest())
    monkeypatch.setattr(setup, "WHISPER_MODEL_SHA256", hashlib.sha256(model).hexdigest())


def downloader_for(archive: bytes, model: bytes, calls: list[str]):
    def download(url: str, destination: Path, progress: object) -> None:
        calls.append(url)
        payload = archive if url.endswith(".zip") else model
        destination.write_bytes(payload)
        progress(len(payload), len(payload))

    return download


def test_pinned_model_uses_the_published_file_digest() -> None:
    # Hugging Face exposes this as X-Linked-ETag. X-Xet-Hash is a storage
    # reconstruction identifier and is not the SHA-256 of downloaded bytes.
    assert setup.WHISPER_MODEL_SHA256 == (
        "c6138d6d58ecc8322097e0f987c32f1be8bb0a18532a3f88f734d1bbf9c41e5d"
    )
    assert setup.WHISPER_MODEL_REVISION in setup.WHISPER_MODEL_URL
    assert "/resolve/main/" not in setup.WHISPER_MODEL_URL


def test_verified_setup_installs_and_reuses_assets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    archive, model = fixture_assets(tmp_path)
    configure_hashes(monkeypatch, archive, model)
    root = tmp_path / "speech"
    calls: list[str] = []
    output: list[str] = []

    executable, model_path = setup.install_local_speech(
        root=root,
        output=output.append,
        downloader=downloader_for(archive, model, calls),
    )

    assert executable.read_bytes() == b"binary"
    assert model_path.read_bytes() == model
    assert setup.assets_are_valid(root)
    assert len(calls) == 2

    setup.install_local_speech(
        root=root,
        output=output.append,
        downloader=downloader_for(archive, model, calls),
    )
    assert len(calls) == 2
    assert any("already installed" in line for line in output)


def test_force_performs_a_new_verified_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    archive, model = fixture_assets(tmp_path)
    configure_hashes(monkeypatch, archive, model)
    root = tmp_path / "speech"
    calls: list[str] = []
    downloader = downloader_for(archive, model, calls)
    setup.install_local_speech(root=root, downloader=downloader)
    setup.install_local_speech(root=root, downloader=downloader, force=True)

    assert len(calls) == 4
    assert setup.assets_are_valid(root)


def test_checksum_failure_leaves_no_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    archive, model = fixture_assets(tmp_path)
    monkeypatch.setattr(setup, "WHISPER_ARCHIVE_SHA256", "0" * 64)
    root = tmp_path / "speech"

    with pytest.raises(SpeechSetupError, match="Checksum"):
        setup.install_local_speech(
            root=root,
            downloader=downloader_for(archive, model, []),
        )

    assert not setup.default_whisper_executable(root).exists()
    assert list(root.glob(".setup-*")) == []


def test_unsafe_zip_entry_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    archive, model = fixture_assets(tmp_path, unsafe=True)
    configure_hashes(monkeypatch, archive, model)

    with pytest.raises(SpeechSetupError, match="unsafe path"):
        setup.install_local_speech(
            root=tmp_path / "speech",
            downloader=downloader_for(archive, model, []),
        )
