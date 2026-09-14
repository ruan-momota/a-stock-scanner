"""四张业务表；旁边的 JSON 仅保存股票名单和最近一次运行状态。"""

import json
from datetime import date, datetime

import duckdb
import pandas as pd

from src import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_prices (
    symbol VARCHAR, date DATE, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
    volume DOUBLE, amount DOUBLE, turnover DOUBLE, PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS stock_pool (
    date DATE, symbol VARCHAR, name VARCHAR, close DOUBLE, position_120 DOUBLE,
    drawdown_120 DOUBLE, distance_low_60 DOUBLE, bottom_score DOUBLE,
    PRIMARY KEY (date, symbol)
);
CREATE TABLE IF NOT EXISTS scan_results (
    timestamp TIMESTAMP, symbol VARCHAR, name VARCHAR, price DOUBLE, change_pct DOUBLE,
    amount DOUBLE, volume_ratio DOUBLE, turnover DOUBLE, day_high DOUBLE, day_low DOUBLE,
    clv DOUBLE, bottom_score DOUBLE, priority_score DOUBLE, PRIMARY KEY (timestamp, symbol)
);
CREATE TABLE IF NOT EXISTS signals (
    timestamp TIMESTAMP, symbol VARCHAR, name VARCHAR, priority_score DOUBLE, note VARCHAR
);
"""
POOL_COLUMNS = [
    "date",
    "symbol",
    "name",
    "close",
    "position_120",
    "drawdown_120",
    "distance_low_60",
    "bottom_score",
]
SCAN_COLUMNS = [
    "timestamp",
    "symbol",
    "name",
    "price",
    "change_pct",
    "amount",
    "volume_ratio",
    "turnover",
    "day_high",
    "day_low",
    "clv",
    "bottom_score",
    "priority_score",
]


def connect():
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(config.DB_PATH))
    connection.execute(SCHEMA)
    return connection


def load_state() -> dict:
    path = config.DB_PATH.with_suffix(".json")
    return json.loads(path.read_text()) if path.exists() else {}


def save_state(**changes):
    state = load_state()
    state.update(changes)
    path = config.DB_PATH.with_suffix(".json")
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str))
    temporary.replace(path)


def save_daily(connection, daily: pd.DataFrame):
    if daily.empty:
        return
    with connection.cursor() as cursor:
        cursor.register("incoming", daily)
        cursor.execute(
            "INSERT OR REPLACE INTO daily_prices BY NAME SELECT * FROM incoming"
        )


def load_daily(connection, symbol: str | None = None) -> pd.DataFrame:
    if symbol is not None:
        return connection.execute(
            "SELECT * FROM daily_prices WHERE symbol = ? ORDER BY date", [symbol]
        ).df()
    return connection.execute("SELECT * FROM daily_prices ORDER BY symbol, date").df()


def save_pool(connection, pool: pd.DataFrame, day: date):
    with connection.cursor() as cursor:
        cursor.begin()
        cursor.execute("DELETE FROM stock_pool WHERE date = ?", [day])
        if not pool.empty:
            cursor.register("incoming", pool[POOL_COLUMNS])
            cursor.execute("INSERT INTO stock_pool BY NAME SELECT * FROM incoming")
        cursor.commit()
    save_state(pool_date=day.isoformat())


def load_pool(connection, day: date | str | None = None) -> pd.DataFrame:
    day = day or load_state().get("pool_date")
    return connection.execute(
        "SELECT * FROM stock_pool WHERE date = ? ORDER BY bottom_score DESC", [day]
    ).df()


def save_scan(
    connection,
    candidates: pd.DataFrame,
    timestamp: datetime,
    pool_date: date,
    pool_size: int,
) -> dict:
    if not candidates.empty:
        rows = candidates.assign(timestamp=timestamp)[SCAN_COLUMNS]
        with connection.cursor() as cursor:
            cursor.register("incoming", rows)
            cursor.execute("INSERT INTO scan_results BY NAME SELECT * FROM incoming")
    metadata = {
        "timestamp": timestamp.isoformat(),
        "pool_date": pool_date.isoformat(),
        "pool_size": pool_size,
        "candidate_count": len(candidates),
    }
    save_state(scan=metadata)
    return metadata


def load_latest_scan(connection) -> tuple[dict, pd.DataFrame]:
    metadata = load_state().get("scan", {})
    rows = connection.execute(
        """SELECT r.*, p.position_120, p.drawdown_120, p.distance_low_60
           FROM scan_results r LEFT JOIN stock_pool p
             ON r.symbol = p.symbol AND p.date = ?
           WHERE r.timestamp = ? ORDER BY r.priority_score DESC""",
        [metadata.get("pool_date"), metadata.get("timestamp")],
    ).df()
    return metadata, rows
