from __future__ import annotations

import hashlib
import io
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageOps, UnidentifiedImageError

from backpack_bot.client import BackpackClient
from backpack_tax.logos import logo_filenames


COINGECKO_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
FMP_STOCK_LOGO_URL = "https://financialmodelingprep.com/image-stock/{ticker}.png"
MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024
USER_AGENT = "Backpack-Tax-Ledger/0.1 local-logo-sync"


@dataclass(frozen=True)
class AutoLogoResult:
    downloaded: int
    skipped: int
    failed: int
    failures: tuple[str, ...]


def _download(url: str, timeout_seconds: float = 20.0) -> bytes:
    response = requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=timeout_seconds,
        stream=True,
    )
    response.raise_for_status()
    content = bytearray()
    for chunk in response.iter_content(64 * 1024):
        content.extend(chunk)
        if len(content) > MAX_DOWNLOAD_BYTES:
            raise ValueError("이미지가 5MB를 초과합니다.")
    return bytes(content)


def _normalized_png(content: bytes) -> bytes:
    Image.MAX_IMAGE_PIXELS = 25_000_000
    try:
        with Image.open(io.BytesIO(content)) as source:
            source.load()
            image = source.convert("RGBA")
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("유효한 이미지가 아닙니다.") from exc
    image = ImageOps.contain(image, (112, 112), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (128, 128), (255, 255, 255, 0))
    canvas.alpha_composite(
        image, ((128 - image.width) // 2, (128 - image.height) // 2)
    )
    output = io.BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _coin_logo_urls(
    backpack_assets: list[dict[str, Any]], coin_symbols: set[str]
) -> dict[str, tuple[str, str]]:
    id_by_symbol: dict[str, str] = {}
    for asset in backpack_assets:
        symbol = str(asset.get("symbol") or "").upper()
        coingecko_id = asset.get("coingeckoId")
        if symbol in coin_symbols and coingecko_id and symbol not in id_by_symbol:
            id_by_symbol[symbol] = str(coingecko_id)
    if not id_by_symbol:
        return {}
    response = requests.get(
        COINGECKO_MARKETS_URL,
        params={
            "vs_currency": "usd",
            "ids": ",".join(sorted(set(id_by_symbol.values()))),
            "per_page": 250,
        },
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    response.raise_for_status()
    image_by_id = {
        str(item["id"]): str(item["image"])
        for item in response.json()
        if item.get("id") and item.get("image")
    }
    return {
        symbol: (image_by_id[coin_id], "CoinGecko")
        for symbol, coin_id in id_by_symbol.items()
        if coin_id in image_by_id
    }


def _write_manifest(
    directory: Path, entries: list[dict[str, str]]
) -> None:
    if not entries:
        return
    manifest_path = directory.parent / "logo_sources.json"
    existing: dict[str, dict[str, str]] = {}
    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (json.JSONDecodeError, OSError):
            existing = {}
    for entry in entries:
        existing[entry["asset"]] = entry
    manifest_path.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def download_missing_asset_logos(
    *,
    directory: Path,
    assets: list[dict[str, Any]],
    backpack_client: BackpackClient | None = None,
) -> AutoLogoResult:
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    existing = logo_filenames(directory)
    unique_assets = {str(item["asset"]).upper(): item for item in assets}
    missing = set(unique_assets) - set(existing)
    coin_symbols = {
        asset
        for asset in missing
        if unique_assets[asset].get("asset_class") != "stock"
    }
    stock_symbols = missing - coin_symbols
    client = backpack_client or BackpackClient()
    urls: dict[str, tuple[str, str]] = {}
    failures: list[str] = []
    try:
        urls.update(_coin_logo_urls(client.get_assets(), coin_symbols))
    except (requests.RequestException, ValueError, TypeError) as exc:
        failures.append(f"코인 로고 목록: {exc}")
    for symbol in stock_symbols:
        ticker = symbol.removesuffix(".US")
        urls[symbol] = (FMP_STOCK_LOGO_URL.format(ticker=ticker), "FMP")
    for symbol in sorted(missing - set(urls)):
        failures.append(f"{symbol}: 로고 출처를 찾지 못했습니다.")

    saved_entries: list[dict[str, str]] = []

    def fetch_one(asset: str, url: str, provider: str) -> tuple[str, bytes, str, str]:
        return asset, _normalized_png(_download(url)), url, provider

    with ThreadPoolExecutor(max_workers=6) as executor:
        jobs = {
            executor.submit(fetch_one, asset, url, provider): asset
            for asset, (url, provider) in urls.items()
        }
        for future in as_completed(jobs):
            asset = jobs[future]
            try:
                _, content, url, provider = future.result()
                target = (directory / f"{asset}.png").resolve()
                if not target.is_relative_to(directory):
                    raise ValueError("안전하지 않은 로고 경로입니다.")
                target.write_bytes(content)
                saved_entries.append(
                    {
                        "asset": asset,
                        "provider": provider,
                        "source_url": url,
                        "retrieved_at_utc": datetime.now(UTC).isoformat(),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                )
            except (requests.RequestException, ValueError, OSError) as exc:
                failures.append(f"{asset}: {exc}")
    _write_manifest(directory, saved_entries)
    return AutoLogoResult(
        downloaded=len(saved_entries),
        skipped=len(existing),
        failed=len(failures),
        failures=tuple(failures),
    )
