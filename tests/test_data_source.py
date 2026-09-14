from datetime import date, timedelta

import pandas as pd
import pytest

from main import main
from src import config, data_source, database, scanner


def test_exchange_list_filters_scope_and_rejects_partial_lists(monkeypatch):
    def sh_list(symbol):
        codes = (
            ["600000", "600001", "600000", "920001"]
            if symbol == "主板A股"
            else ["688001"]
        )
        names = (
            ["浦发银行", "ST测试", "浦发银行", "北交所"]
            if symbol == "主板A股"
            else ["华兴源创"]
        )
        return pd.DataFrame({"证券代码": codes, "证券简称": names})

    monkeypatch.setattr(data_source.ak, "stock_info_sh_name_code", sh_list)
    monkeypatch.setattr(
        data_source.ak,
        "stock_info_sz_name_code",
        lambda symbol: pd.DataFrame(
            {
                "A股代码": ["000001", "300001", "000002"],
                "A股简称": ["平安银行", "特锐德", "退市测试"],
            }
        ),
    )
    stocks = data_source.get_stock_list("sina")
    assert stocks.symbol.tolist() == ["600000", "688001", "000001", "300001"]
    assert stocks.market.tolist() == ["沪市主板", "科创板", "深市主板", "创业板"]
    monkeypatch.setattr(
        data_source.ak, "stock_info_sz_name_code", lambda symbol: pd.DataFrame()
    )
    with pytest.raises(ValueError, match="交易所股票名单为空"):
        data_source.get_stock_list("sina")


@pytest.mark.parametrize(
    "symbol,prefix",
    [("600000", "sh"), ("000001", "sz"), ("300001", "sz"), ("688001", "sh")],
)
def test_sina_daily_preserves_prices_and_converts_units(monkeypatch, symbol, prefix):
    calls = []

    def daily(**kwargs):
        calls.append(kwargs)
        return pd.DataFrame(
            {
                "date": ["2026-09-10", "2026-09-11"],
                "open": [10.0, 10.0],
                "high": [12.0, 12.0],
                "low": [9.0, 9.0],
                "close": [11.0, 30.0],
                "volume": [10000.0, 20000.0],
                "amount": [110000.0, 220000.0],
                "turnover": [0.025, 0.05],
                "outstanding_share": [400000.0, 400000.0],
            }
        )

    monkeypatch.setattr(data_source.ak, "stock_zh_a_daily", daily)
    rows = data_source.get_daily_history(
        symbol, date(2026, 9, 10), date(2026, 9, 11), "sina"
    )
    assert calls == [
        {
            "symbol": prefix + symbol,
            "adjust": "qfq",
            "start_date": "20260910",
            "end_date": "20260911",
        }
    ]
    assert len(rows) == 1
    row = rows.iloc[0]
    assert row.symbol == symbol
    assert row.close == 11.0
    assert row.volume == 100.0
    assert row.amount == 110000.0
    assert row.turnover == 2.5


def test_source_is_persisted_reused_and_cannot_mix_prices(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "market.duckdb")
    day = date(2026, 9, 11)
    monkeypatch.setattr(scanner, "last_completed_day", lambda _: day)
    monkeypatch.setattr(data_source, "get_trading_days", lambda: (day,))
    monkeypatch.setattr(data_source.time, "sleep", lambda _: None)
    sources = []

    def stocks(source):
        sources.append(source)
        return pd.DataFrame(
            [{"symbol": "600000", "name": "浦发银行", "market": "沪市主板"}]
        )

    monkeypatch.setattr(data_source, "get_stock_list", stocks)
    monkeypatch.setattr(
        data_source.ak,
        "stock_zh_a_daily",
        lambda **_: pd.DataFrame(
            {
                "date": [day],
                "open": [10.0],
                "high": [12.0],
                "low": [9.0],
                "close": [11.0],
                "volume": [10000.0],
                "amount": [110000.0],
                "turnover": [0.025],
            }
        ),
    )
    assert main(["init", "--source", "sina"]) == 0
    assert main(["update"]) == 0
    assert database.load_state()["history_source"] == "sina"
    assert sources == ["sina"]
    with database.connect() as connection:
        rows = database.load_daily(connection)
        assert len(rows) == 1 and rows.iloc[0].volume == 100.0
        with pytest.raises(ValueError, match="混合前复权"):
            data_source.update_history(connection, day, source="eastmoney")
    assert database.load_state()["history_source"] == "sina"
    assert sources == ["sina"]


def test_update_appends_one_market_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "market.duckdb")
    day = date(2026, 9, 11)
    previous = day - timedelta(days=1)
    stocks = pd.DataFrame(
        [
            {"symbol": "600000", "name": "已完成", "market": "沪市主板"},
            {"symbol": "000001", "name": "待更新", "market": "深市主板"},
        ]
    )

    def row(symbol, row_day):
        return pd.DataFrame(
            {
                "symbol": [symbol],
                "date": [row_day],
                "open": [10.0],
                "high": [12.0],
                "low": [9.0],
                "close": [11.0],
                "volume": [100.0],
                "amount": [110000.0],
                "turnover": [2.5],
            }
        )

    snapshots = []

    def snapshot():
        snapshots.append(1)
        return pd.DataFrame(
            {
                "symbol": ["000001"],
                "price": [11.0],
                "open": [10.0],
                "day_high": [12.0],
                "day_low": [9.0],
                "volume": [100.0],
                "amount": [110000.0],
                "turnover": [2.5],
            }
        )

    monkeypatch.setattr(data_source, "get_realtime_quotes", snapshot)
    with database.connect() as connection:
        database.save_daily(connection, row("600000", day))
        database.save_daily(connection, row("000001", previous))
        database.save_state(history_source="sina", stocks=stocks.to_dict("records"))
        result = data_source.update_history(connection, day)
        rows = database.load_daily(connection, "000001")

    assert snapshots == [1]
    assert rows.date.dt.date.tolist() == [previous, day]
    assert result == {
        "date": day.isoformat(),
        "saved": 1,
        "failed": {},
        "skipped": 1,
    }


def test_eastmoney_list_failure_explains_initialization_stage(monkeypatch):
    def unavailable():
        raise ConnectionError("remote closed")

    monkeypatch.setattr(data_source, "get_realtime_quotes", unavailable)
    with pytest.raises(RuntimeError, match="尚未开始下载日线") as error:
        data_source.get_stock_list("eastmoney")
    assert isinstance(error.value.__cause__, ConnectionError)
