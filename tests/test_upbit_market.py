from __future__ import annotations

from pathlib import Path

import backpack_tax.app as app_module
import backpack_tax.upbit_market as market_module
from backpack_tax.app import create_app


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.payload


def test_upbit_ticker_and_history_are_normalized(monkeypatch) -> None:
    def fake_get(url, **_kwargs):
        if url == market_module.UPBIT_TICKER_URL:
            return FakeResponse(
                [
                    {
                        "trade_price": 1412.5,
                        "signed_change_rate": 0.0021,
                        "timestamp": 1787533200000,
                    }
                ]
            )
        return FakeResponse(
            [
                {
                    "candle_date_time_utc": "2026-08-24T01:01:00",
                    "opening_price": 1411.0,
                    "high_price": 1413.0,
                    "low_price": 1410.5,
                    "trade_price": 1412.0,
                },
                {
                    "candle_date_time_utc": "2026-08-24T01:00:00",
                    "opening_price": 1410.0,
                    "high_price": 1412.0,
                    "low_price": 1409.5,
                    "trade_price": 1411.0,
                },
            ]
        )

    monkeypatch.setattr(market_module.requests, "get", fake_get)

    ticker = market_module.fetch_upbit_usdt_ticker()
    history = market_module.fetch_upbit_usdt_history(count=60)

    assert ticker["market"] == "KRW-USDT"
    assert ticker["price"] == 1412.5
    assert ticker["signed_change_rate"] == 0.0021
    assert [point["price"] for point in history["points"]] == [1411.0, 1412.0]
    assert history["candles"][0] == {
        "timestamp": 1787533200000,
        "open": 1410.0,
        "high": 1412.0,
        "low": 1409.5,
        "close": 1411.0,
    }
    assert history["unit"] == "1m"


def test_dashboard_renders_live_usdt_chart_and_market_routes(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        app_module,
        "fetch_upbit_usdt_history",
        lambda count=60: {
            "market": "KRW-USDT",
            "unit": "1m",
            "candles": [
                {
                    "timestamp": 1,
                    "open": 1410.0,
                    "high": 1411.0,
                    "low": 1409.0,
                    "close": 1410.5,
                }
            ],
            "points": [{"timestamp": 1, "price": 1410.0}],
            "source_url": "https://api.upbit.com/history",
        },
    )
    monkeypatch.setattr(
        app_module,
        "fetch_upbit_usdt_ticker",
        lambda: {
            "market": "KRW-USDT",
            "price": 1412.5,
            "signed_change_rate": 0.0021,
            "timestamp": 2,
            "source_url": "https://api.upbit.com/ticker",
        },
    )
    app = create_app(project_root=tmp_path, testing=True)
    client = app.test_client()

    dashboard = client.get("/")
    history = client.get("/api/market/upbit/usdt/history")
    ticker = client.get("/api/market/upbit/usdt/ticker")

    assert dashboard.status_code == 200
    assert "<title>대시보드 · 코인어카운트</title>".encode() in dashboard.data
    assert "<strong>코인어카운트</strong>".encode() in dashboard.data
    assert b'src="/static/coinaccount-logo-v3.png"' in dashboard.data
    assert 'alt="코인어카운트 CA 로고"'.encode() in dashboard.data
    logo = client.get("/static/coinaccount-logo-v3.png")
    assert logo.status_code == 200
    assert logo.content_type == "image/png"
    assert "증빙 처리 단계".encode() not in dashboard.data
    assert b"recent-imports-panel" in dashboard.data
    assert "업비트 테더 원화 가격".encode() in dashboard.data
    assert b'class="usdt-market-controls"' in dashboard.data
    assert b'id="usdt-price-chart"' in dashboard.data
    assert b'id="usdt-chart-time-axis"' in dashboard.data
    assert "한국시간(KST)".encode() in dashboard.data
    assert b'timeZone: "Asia/Seoul"' in dashboard.data
    assert b"const tickIndexes = [0, 15, 30, 45, candles.length - 1]" in dashboard.data
    assert b'data-chart-view="candle"' in dashboard.data
    assert b'id="usdt-candle-layer"' in dashboard.data
    assert b"mergeLivePrice" in dashboard.data
    assert b'candleLayer.toggleAttribute("hidden", lineView)' in dashboard.data
    assert b'lineElement.toggleAttribute("hidden", !lineView)' in dashboard.data
    assert b"candleLayer.hidden = lineView" not in dashboard.data
    assert b"window.setInterval(loadTicker, 3000)" in dashboard.data
    assert history.status_code == 200
    assert history.json["points"][0]["price"] == 1410.0
    assert ticker.status_code == 200
    assert ticker.json["price"] == 1412.5


def test_market_route_returns_safe_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        app_module,
        "fetch_upbit_usdt_ticker",
        lambda: (_ for _ in ()).throw(ValueError("private upstream detail")),
    )
    app = create_app(project_root=tmp_path, testing=True)
    response = app.test_client().get("/api/market/upbit/usdt/ticker")

    assert response.status_code == 502
    assert response.json == {"error": "업비트 현재가를 불러오지 못했습니다."}
    assert b"private upstream detail" not in response.data
