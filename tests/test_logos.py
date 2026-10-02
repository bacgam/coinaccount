from __future__ import annotations

import io
from pathlib import Path

import pytest

from backpack_tax.app import create_app
from backpack_tax.logos import LogoUpload, save_asset_logos


def test_logo_upload_uses_asset_ticker_and_is_served_locally(tmp_path: Path) -> None:
    app = create_app(project_root=tmp_path, testing=True)
    client = app.test_client()
    png = b"\x89PNG\r\n\x1a\n" + b"local-logo"

    response = client.post(
        "/assets/logos",
        data={"logo_files": (io.BytesIO(png), "btc.png")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert (tmp_path / "data" / "tax_assets" / "logos" / "BTC.png").read_bytes() == png
    served = client.get("/assets/logos/BTC.png")
    assert served.status_code == 200
    assert served.data == png


def test_svg_logo_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="PNG, JPG, WEBP, GIF"):
        save_asset_logos(
            tmp_path,
            [LogoUpload(filename="BTC.svg", stream=io.BytesIO(b"<svg></svg>"))],
        )


def test_oversized_request_returns_korean_guidance(tmp_path: Path) -> None:
    app = create_app(project_root=tmp_path, testing=True)
    app.config["MAX_CONTENT_LENGTH"] = 32
    client = app.test_client()

    response = client.post(
        "/assets/logos",
        data={"logo_files": (io.BytesIO(b"x" * 100), "BTC.png")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "200MB".encode() in response.data
