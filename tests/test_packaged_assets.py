from __future__ import annotations

import importlib.util
import re
import shutil
from pathlib import Path
from zipfile import ZipFile

from backpack_tax.app import create_app


def test_product_zip_serves_every_stylesheet_asset(tmp_path: Path) -> None:
    project = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("product_package", project / "build_product_package.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    staging = tmp_path / "source"
    staging.mkdir()
    for filename in module.ROOT_FILES:
        shutil.copyfile(project / filename, staging / filename)
    for package in ("backpack_tax", "backpack_bot"):
        shutil.copytree(project / "src" / package, staging / "src" / package,
                        ignore=shutil.ignore_patterns("__pycache__"))
    (staging / "tests").mkdir()
    module.build_product_package(staging)

    extracted = tmp_path / "extracted"
    with ZipFile(staging / "dist" / "coinaccount-full-product.zip") as archive:
        archive.extractall(extracted)
    static = extracted / "src" / "backpack_tax" / "static"
    app = create_app(project_root=tmp_path / "runtime", testing=True)
    app.static_folder = str(static)
    client = app.test_client()
    asset_urls = set()
    for stylesheet in static.rglob("*.css"):
        asset_urls.update(re.findall(r"/static/[^\s\"')]+", stylesheet.read_text(encoding="utf-8")))
    assert {f"/static/exchange-logos/{name}.jpg" for name in ("backpack", "upbit", "bithumb")} <= asset_urls
    for url in asset_urls:
        response = client.get(url)
        assert response.status_code == 200, url
        assert response.data == (static / url.removeprefix("/static/")).read_bytes()
    for name in ("backpack", "upbit", "bithumb"):
        response = client.get(f"/static/exchange-logos/{name}.jpg")
        assert response.mimetype == "image/jpeg"
        assert response.data.startswith(b"\xff\xd8\xff")
