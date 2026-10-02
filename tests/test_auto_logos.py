from __future__ import annotations

import io
import json
from pathlib import Path

from PIL import Image

from backpack_tax.auto_logos import download_missing_asset_logos


def _png_bytes(color: tuple[int, int, int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGBA", (32, 20), color).save(output, format="PNG")
    return output.getvalue()


def test_auto_logo_download_only_fills_missing_assets(monkeypatch, tmp_path: Path) -> None:
    bitcoin = _png_bytes((255, 140, 0, 255))
    nvidia = _png_bytes((80, 180, 20, 255))
    existing = _png_bytes((40, 40, 40, 255))
    (tmp_path / "ETH.png").write_bytes(existing)

    class FakeClient:
        def get_assets(self):
            return [{"symbol": "BTC", "coingeckoId": "bitcoin"}]

    class FakeResponse:
        def __init__(self, *, data=None, content=b""):
            self.data = data
            self.content = content

        def raise_for_status(self):
            return None

        def json(self):
            return self.data

        def iter_content(self, _size):
            yield self.content

    def fake_get(url, **_kwargs):
        if url.endswith("/coins/markets"):
            return FakeResponse(
                data=[{"id": "bitcoin", "image": "https://images.test/btc.png"}]
            )
        if url.endswith("btc.png"):
            return FakeResponse(content=bitcoin)
        if url.endswith("NVDA.png"):
            return FakeResponse(content=nvidia)
        raise AssertionError(url)

    monkeypatch.setattr("backpack_tax.auto_logos.requests.get", fake_get)
    result = download_missing_asset_logos(
        directory=tmp_path,
        assets=[
            {"asset": "BTC", "asset_class": "coin"},
            {"asset": "ETH", "asset_class": "coin"},
            {"asset": "NVDA.US", "asset_class": "stock"},
        ],
        backpack_client=FakeClient(),
    )

    assert result.downloaded == 2
    assert result.failed == 0
    assert (tmp_path / "ETH.png").read_bytes() == existing
    with Image.open(tmp_path / "BTC.png") as image:
        assert image.size == (128, 128)
    with Image.open(tmp_path / "NVDA.US.png") as image:
        assert image.size == (128, 128)
    manifest = json.loads((tmp_path.parent / "logo_sources.json").read_text("utf-8"))
    assert manifest["BTC"]["provider"] == "CoinGecko"
    assert manifest["NVDA.US"]["provider"] == "FMP"
