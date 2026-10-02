from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
ASSET_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,29}$")
MAX_LOGO_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class LogoUpload:
    filename: str
    stream: BinaryIO


def _valid_signature(extension: str, content: bytes) -> bool:
    if extension == ".png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if extension in {".jpg", ".jpeg"}:
        return content.startswith(b"\xff\xd8\xff")
    if extension == ".webp":
        return content.startswith(b"RIFF") and content[8:12] == b"WEBP"
    if extension == ".gif":
        return content.startswith((b"GIF87a", b"GIF89a"))
    return False


def save_asset_logos(directory: Path, uploads: list[LogoUpload]) -> list[str]:
    if not uploads:
        raise ValueError("로고 파일을 하나 이상 선택하세요.")
    prepared: list[tuple[str, bytes]] = []
    seen_assets: set[str] = set()
    for upload in uploads:
        safe_name = Path(upload.filename).name
        extension = Path(safe_name).suffix.lower()
        asset = Path(safe_name).stem.upper()
        if extension not in ALLOWED_EXTENSIONS:
            raise ValueError(f"{safe_name}: PNG, JPG, WEBP, GIF만 사용할 수 있습니다.")
        if not ASSET_PATTERN.fullmatch(asset):
            raise ValueError(f"{safe_name}: 파일명을 BTC.png처럼 자산 티커로 지정하세요.")
        if asset in seen_assets:
            raise ValueError(f"{asset}: 한 번의 업로드에서 같은 자산이 중복되었습니다.")
        content = upload.stream.read(MAX_LOGO_BYTES + 1)
        if not content or len(content) > MAX_LOGO_BYTES:
            raise ValueError(f"{safe_name}: 파일은 2MB 이하여야 합니다.")
        if not _valid_signature(extension, content):
            raise ValueError(f"{safe_name}: 확장자와 실제 이미지 형식이 일치하지 않습니다.")
        seen_assets.add(asset)
        prepared.append((f"{asset}{extension}", content))

    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for filename, content in prepared:
        target = (directory / filename).resolve()
        if not target.is_relative_to(directory):
            raise ValueError("안전하지 않은 로고 파일 경로입니다.")
        target.write_bytes(content)
        saved.append(filename)
    return saved


def logo_filenames(directory: Path) -> dict[str, str]:
    directory = directory.resolve()
    if not directory.is_dir():
        return {}
    result: dict[str, tuple[int, str]] = {}
    for path in directory.iterdir():
        if not path.is_file() or path.suffix.lower() not in ALLOWED_EXTENSIONS:
            continue
        asset = path.stem.upper()
        modified = path.stat().st_mtime_ns
        if asset not in result or modified > result[asset][0]:
            result[asset] = (modified, path.name)
    return {asset: value[1] for asset, value in result.items()}
