from datetime import datetime, timedelta

import pandas as pd
import pytest
from test_features import history

from main import main
from src import config, data_source, database, scanner


def test_workflow(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "market.duckdb")
    daily = history(180)
    daily.loc[119, "volume"] = 200.0
    day = daily.date.iloc[-1].date()
    now = datetime.combine(day + timedelta(days=1), datetime.min.time()).replace(
        hour=10, tzinfo=config.TIMEZONE
    )
    monkeypatch.setattr(scanner, "last_completed_day", lambda _: day)
    monkeypatch.setattr(
        data_source, "get_trading_days", lambda: (*daily.date.dt.date, now.date())
    )
    stocks = pd.DataFrame(
        [{"symbol": "600000", "name": "测试股票", "market": "沪市主板"}]
    )
    monkeypatch.setattr(data_source, "get_stock_list", lambda source: stocks)
    downloads = []

    def download(symbol, start, end, source):
        downloads.append((symbol, start, end))
        return daily.copy()

    monkeypatch.setattr(data_source, "get_daily_history", download)
    assert main(["init"]) == 0
    assert main(["update"]) == 0
    assert len(downloads) == 1
    assert downloads[-1][1] == daily.date.iloc[0].date()
    assert main(["pool"]) == 0
    assert main(["backtest"]) == 0
    assert (tmp_path / "backtest" / "summary.csv").exists()
    quotes = pd.DataFrame(
        [
            {
                "symbol": "600000",
                "name": "测试股票",
                "price": 11.0,
                "change_pct": 0.0,
                "amount": 1e8,
                "volume_ratio": 2.0,
                "turnover": 2.0,
                "day_high": 12.0,
                "day_low": 10.0,
            }
        ]
    )
    calls = []

    def realtime(*_):
        calls.append(1)
        return quotes.copy()

    monkeypatch.setattr(data_source, "get_realtime_quotes", realtime)
    with database.connect() as connection:
        assert len(database.load_daily(connection)) == 180
        metadata, candidates = scanner.scan_once(connection, now)
        assert len(candidates) == 1
        saved, rows = database.load_latest_scan(connection)
        assert saved == metadata
        assert rows.iloc[0].position_120 == candidates.iloc[0].position_120
        assert scanner.scan_once(connection, now.replace(hour=12))[0] == metadata
        assert len(calls) == 1

        def unavailable(*_):
            raise ConnectionError("行情接口不可用")

        monkeypatch.setattr(data_source, "get_realtime_quotes", unavailable)
        with pytest.raises(ConnectionError):
            scanner.scan_once(connection, now + timedelta(seconds=30))
        assert database.load_latest_scan(connection)[0] == metadata
        monkeypatch.setattr(data_source, "get_realtime_quotes", realtime)
        quotes.loc[0, "volume_ratio"] = 1.0
        empty_meta, empty = scanner.scan_once(connection, now + timedelta(minutes=1))
        assert empty.empty and empty_meta["candidate_count"] == 0
        assert database.load_latest_scan(connection)[1].empty
        assert (
            connection.execute("SELECT count(*) FROM scan_results").fetchone()[0] == 1
        )
        phases = iter(["交易中", "已收盘"])
        monkeypatch.setattr(scanner, "market_phase", lambda _: next(phases))
        scans = []
        monkeypatch.setattr(scanner, "scan_once", lambda *_: scans.append(1))
        monkeypatch.setattr(scanner.time, "sleep", lambda _: None)
        scanner.run_auto_scan(connection)
        assert scans == [1]


def test_page_manual_does_not_request_quotes(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "market.duckdb")

    def forbidden(*_):
        raise AssertionError("手动打开页面不应请求行情")

    monkeypatch.setattr(data_source, "get_realtime_quotes", forbidden)
    app = AppTest.from_file(str(config.ROOT / "app.py")).run(timeout=15)
    assert not app.exception
    assert not app.error
    assert app.sidebar.radio[0].value == "手动"
    app.number_input[0].set_value(2.0).run()
    assert not app.exception
    assert not app.error


def test_page_scan_detail_and_mode_switch(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "market.duckdb")
    daily = history()
    day = daily.date.iloc[-1].date()
    with database.connect() as connection:
        database.save_daily(connection, daily)
        database.save_state(stocks=[{"symbol": "600000", "name": "测试股票"}])
        pool = scanner.build_pool(connection, day)
    calls = []

    def scan(connection, now):
        calls.append(now)
        rows = pool.assign(
            price=11.0,
            change_pct=1.0,
            volume_ratio=2.0,
            amount=1e8,
            turnover=2.0,
            day_high=12.0,
            day_low=10.0,
            clv=0.5,
            priority_score=60.0,
        )
        metadata = database.save_scan(
            connection, rows, now.replace(tzinfo=None), day, 1
        )
        return metadata, rows

    monkeypatch.setattr(scanner, "market_phase", lambda _: "交易中")
    monkeypatch.setattr(scanner, "scan_once", scan)
    app = AppTest.from_file(str(config.ROOT / "app.py")).run(timeout=15)
    app.button[0].click().run()
    assert len(calls) == 1
    assert not app.error and not app.exception
    app.selectbox[0].select("600000").run()
    assert len(app.get("plotly_chart")) == 1
    assert len(calls) == 1
    app.sidebar.radio[0].set_value("自动").run()
    assert len(calls) == 2
    app.session_state.next_scan = 0
    app.run()
    assert len(calls) == 3
    assert len(app.get("plotly_chart")) == 1
    app.sidebar.radio[0].set_value("手动").run()
    app.number_input[0].set_value(2.1).run()
    assert len(calls) == 3
    assert not app.error and not app.exception
