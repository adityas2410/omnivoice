"""Explicit, verified installer for the pinned local whisper.cpp assets."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import urllib.request
import uuid
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath


WHISPER_CPP_RELEASE = "b5130"
WHISPER_ARCHIVE_NAME = "whisper-blas-bin-x64.zip"
WHISPER_ARCHIVE_URL = (
    "https://github.com/ggml-org/whisper.cpp/releases/download/"
    f"{WHISPER_CPP_RELEASE}/{WHISPER_ARCHIVE_NAME}"
)
WHISPER_ARCHIVE_SHA256 = (
    "55c06d09e8b9b6cfb2b0b47ddedc71803054f0e48be1f41848b3141c06c703a9"
)
WHISPER_MODEL_NAME = "ggml-small.en.bin"
WHISPER_MODEL_REVISION = "5359861c739e955e79d9a303bcbc70fb988958b1"
WHISPER_MODEL_URL = (
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/"
    f"{WHISPER_MODEL_REVISION}/"
    f"{WHISPER_MODEL_NAME}"
)
WHISPER_MODEL_SHA256 = (
    "c6138d6d58ecc8322097e0f987c32f1be8bb0a18532a3f88f734d1bbf9c41e5d"
)
MANIFEST_NAME = "omnivoice-manifest.json"


class SpeechSetupError(RuntimeError):
    """Raised when verified local speech assets cannot be installed."""


def default_speech_directory() -> Path:
    """Return the per-user location used for downloaded local speech assets."""

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "OmniVoice" / "speech"
    return Path.home() / "AppData" / "Local" / "OmniVoice" / "speech"


def default_whisper_executable(root: Path | None = None) -> Path:
    """Return the pinned whisper.cpp command path."""

    base = root or default_speech_directory()
    return base / f"whisper.cpp-{WHISPER_CPP_RELEASE}" / "whisper-cli.exe"


def default_whisper_model(root: Path | None = None) -> Path:
    """Return the pinned English model path."""

    base = root or default_speech_directory()
    return base / "models" / WHISPER_MODEL_NAME


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Hash a file without loading a model-sized object into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def install_local_speech(
    *,
    force: bool = False,
    root: Path | None = None,
    output: Callable[[str], None] = print,
    downloader: Callable[[str, Path, Callable[[int, int | None], None]], None]
    | None = None,
) -> tuple[Path, Path]:
    """Download, verify, and atomically place the pinned binary and model."""

    install_root = (root or default_speech_directory()).resolve()
    executable = default_whisper_executable(install_root)
    model = default_whisper_model(install_root)
    if not force and assets_are_valid(install_root):
        output("Local speech assets are already installed and verified.")
        return executable, model

    install_root.mkdir(parents=True, exist_ok=True)
    download = downloader or _download
    temporary = Path(tempfile.mkdtemp(prefix=".setup-", dir=install_root))
    try:
        archive_path = temporary / WHISPER_ARCHIVE_NAME
        model_path = temporary / WHISPER_MODEL_NAME
        output("Downloading whisper.cpp...")
        download(WHISPER_ARCHIVE_URL, archive_path, _progress(output, "whisper.cpp"))
        _require_digest(archive_path, WHISPER_ARCHIVE_SHA256, "whisper.cpp archive")

        output("Downloading small.en model...")
        download(WHISPER_MODEL_URL, model_path, _progress(output, "small.en model"))
        _require_digest(model_path, WHISPER_MODEL_SHA256, "small.en model")

        extracted = temporary / "extracted"
        _safe_extract(archive_path, extracted)
        matches = list(extracted.rglob("whisper-cli.exe"))
        if len(matches) != 1:
            raise SpeechSetupError("The verified archive did not contain one whisper-cli.exe")

        # Keep the executable and adjacent BLAS/runtime DLLs together. This is
        # the layout whisper.cpp expects when started from an arbitrary directory.
        payload = temporary / "payload"
        shutil.copytree(matches[0].parent, payload)
        _write_manifest(payload)

        executable.parent.parent.mkdir(parents=True, exist_ok=True)
        model.parent.mkdir(parents=True, exist_ok=True)
        _replace_directory(payload, executable.parent)
        os.replace(model_path, model)
        if not assets_are_valid(install_root):
            raise SpeechSetupError("Installed speech assets failed final verification")
        output("Local speech assets installed and verified.")
        return executable, model
    except SpeechSetupError:
        raise
    except BaseException as exc:
        raise SpeechSetupError("Local speech setup did not complete") from exc
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def assets_are_valid(root: Path | None = None) -> bool:
    """Verify the model and every file recorded from the trusted release ZIP."""

    install_root = (root or default_speech_directory()).resolve()
    executable = default_whisper_executable(install_root)
    model = default_whisper_model(install_root)
    manifest_path = executable.parent / MANIFEST_NAME
    if not executable.is_file() or not model.is_file() or not manifest_path.is_file():
        return False
    try:
        if sha256_file(model) != WHISPER_MODEL_SHA256:
            return False
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        files = manifest["files"]
        if not isinstance(files, dict):
            return False
        for relative, expected in files.items():
            candidate = (executable.parent / relative).resolve()
            if not candidate.is_relative_to(executable.parent.resolve()):
                return False
            if not candidate.is_file() or sha256_file(candidate) != expected:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False


def _download(
    url: str,
    destination: Path,
    progress: Callable[[int, int | None], None],
) -> None:
    """Stream one URL into a temporary file while reporting byte progress."""

    request = urllib.request.Request(url, headers={"User-Agent": "OmniVoice/0.1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw_total = response.headers.get("Content-Length")
        total = int(raw_total) if raw_total and raw_total.isdigit() else None
        received = 0
        with destination.open("wb") as handle:
            while chunk := response.read(1024 * 1024):
                handle.write(chunk)
                received += len(chunk)
                progress(received, total)


def _progress(output: Callable[[str], None], label: str) -> Callable[[int, int | None], None]:
    last_percent = -10

    def report(received: int, total: int | None) -> None:
        nonlocal last_percent
        if total:
            percent = int(received * 100 / total)
            if percent >= last_percent + 10 or percent == 100:
                last_percent = percent
                output(f"{label}: {percent}%")
        elif received // (25 * 1024 * 1024) > last_percent:
            last_percent = received // (25 * 1024 * 1024)
            output(f"{label}: {received // (1024 * 1024)} MiB")

    return report


def _require_digest(path: Path, expected: str, label: str) -> None:
    if sha256_file(path) != expected:
        raise SpeechSetupError(f"Checksum verification failed for {label}")


def _safe_extract(archive_path: Path, destination: Path) -> None:
    """Reject absolute, parent-traversing, and drive-qualified ZIP members."""

    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                pure = PurePosixPath(member.filename.replace("\\", "/"))
                if pure.is_absolute() or ".." in pure.parts or ":" in member.filename:
                    raise SpeechSetupError("The whisper.cpp archive contains an unsafe path")
                target = (destination.joinpath(*pure.parts)).resolve()
                if not target.is_relative_to(root):
                    raise SpeechSetupError("The whisper.cpp archive contains an unsafe path")
            archive.extractall(destination)
    except zipfile.BadZipFile as exc:
        raise SpeechSetupError("The whisper.cpp archive is not a valid ZIP file") from exc


def _write_manifest(directory: Path) -> None:
    files = {
        str(path.relative_to(directory)).replace("\\", "/"): sha256_file(path)
        for path in sorted(directory.rglob("*"))
        if path.is_file() and path.name != MANIFEST_NAME
    }
    manifest = {
        "release": WHISPER_CPP_RELEASE,
        "archive_sha256": WHISPER_ARCHIVE_SHA256,
        "files": files,
    }
    (directory / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _replace_directory(source: Path, destination: Path) -> None:
    """Swap a verified directory into place and roll back a failed rename."""

    backup = destination.with_name(f"{destination.name}.backup-{uuid.uuid4().hex}")
    had_destination = destination.exists()
    if had_destination:
        os.replace(destination, backup)
    try:
        os.replace(source, destination)
    except BaseException:
        if had_destination and backup.exists():
            os.replace(backup, destination)
        raise
    if backup.exists():
        shutil.rmtree(backup)
