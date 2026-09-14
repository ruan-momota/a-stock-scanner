"""只获取和整理 AKShare 数据，不包含选股规则。"""

import logging
import time
from datetime import date
from functools import lru_cache

import akshare as ak
import numpy as np
import pandas as pd

from src import config

logger = logging.getLogger(__name__)

QUOTE_COLUMNS = {
    "代码": "symbol",
    "名称": "name",
    "最新价": "price",
    "涨跌幅": "change_pct",
    "成交量": "volume",
    "成交额": "amount",
    "最高": "day_high",
    "最低": "day_low",
    "量比": "volume_ratio",
    "换手率": "turnover",
    "总市值": "market_cap",
    "流通市值": "float_market_cap",
    "5分钟涨跌": "change_5m",
}
DAILY_COLUMNS = {
    "股票代码": "symbol",
    "日期": "date",
    "开盘": "open",
    "最高": "high",
    "最低": "low",
    "收盘": "close",
    "成交量": "volume",
    "成交额": "amount",
    "换手率": "turnover",
}


def request(function, **kwargs):
    for attempt in range(config.REQUEST_RETRIES + 1):
        try:
            return function(**kwargs)
        except Exception as exc:
            if attempt == config.REQUEST_RETRIES:
                raise
            logger.warning(
                "%s 请求失败（%s），重试 %s/%s",
                function.__name__,
                exc,
                attempt + 1,
                config.REQUEST_RETRIES,
            )
            time.sleep(1)


def get_realtime_quotes() -> pd.DataFrame:
    raw = request(ak.stock_zh_a_spot_em)
    required = {"代码", "名称", "最新价", "成交额", "量比", "最高", "最低", "换手率"}
    if raw.empty or not required.issubset(raw.columns):
        raise ValueError("实时行情为空或缺少关键字段，请稍后重试。")
    quotes = raw.rename(columns=QUOTE_COLUMNS).reindex(columns=QUOTE_COLUMNS.values())
    quotes["symbol"] = quotes["symbol"].astype(str).str.zfill(6)
    for column in quotes.columns.difference(["symbol", "name"]):
        quotes[column] = pd.to_numeric(quotes[column], errors="coerce")
    logger.info("获取实时行情成功，共 %s 只", len(quotes))
    return quotes.drop_duplicates("symbol")


def eligible_stocks(quotes: pd.DataFrame) -> pd.DataFrame:
    symbols = quotes["symbol"]
    allowed = symbols.str.fullmatch(r"(?:60\d{4}|688\d{3}|00\d{4}|30[01]\d{3})")
    excluded = quotes["name"].str.contains("ST|退", case=False, na=True)
    stocks = quotes.loc[allowed & ~excluded].copy()
    stocks["market"] = np.select(
        [
            stocks.symbol.str.startswith("688"),
            stocks.symbol.str.startswith("30"),
            stocks.symbol.str.startswith("60"),
        ],
        ["科创板", "创业板", "沪市主板"],
        default="深市主板",
    )
    return stocks


def get_stock_list(source: str = "eastmoney") -> pd.DataFrame:
    if source == "sina":
        frames = []
        for function, board, code_column, name_column in (
            (ak.stock_info_sh_name_code, "主板A股", "证券代码", "证券简称"),
            (ak.stock_info_sh_name_code, "科创板", "证券代码", "证券简称"),
            (ak.stock_info_sz_name_code, "A股列表", "A股代码", "A股简称"),
        ):
            raw = request(function, symbol=board)
            if raw.empty or not {code_column, name_column}.issubset(raw.columns):
                raise ValueError(f"交易所股票名单为空或缺少代码、名称：{board}")
            frames.append(
                raw[[code_column, name_column]].rename(
                    columns={code_column: "symbol", name_column: "name"}
                )
            )
        stocks = pd.concat(frames, ignore_index=True)
        stocks["symbol"] = stocks["symbol"].astype(str).str.zfill(6)
    elif source == "eastmoney":
        try:
            stocks = get_realtime_quotes()
        except Exception as exc:
            raise RuntimeError(
                "东方财富股票名单获取失败，尚未开始下载日线。"
                "可尝试 init --source sina 使用新浪初始化；盘中扫描仍依赖东方财富。"
            ) from exc
    else:
        raise ValueError(f"未知历史数据源：{source}")
    return eligible_stocks(stocks)[["symbol", "name", "market"]].drop_duplicates(
        "symbol"
    )


def get_daily_history(
    symbol: str, start: date, end: date, source: str = "eastmoney"
) -> pd.DataFrame:
    dates = {"start_date": start.strftime("%Y%m%d"), "end_date": end.strftime("%Y%m%d")}
    if source == "sina":
        raw = request(
            ak.stock_zh_a_daily,
            symbol=("sh" if symbol.startswith("6") else "sz") + symbol,
            adjust="qfq",
            **dates,
        ).copy()
        if not raw.empty:
            raw["symbol"] = symbol
            raw["volume"] = pd.to_numeric(raw["volume"], errors="coerce") / 100
            raw["turnover"] = pd.to_numeric(raw["turnover"], errors="coerce") * 100
    elif source == "eastmoney":
        raw = request(
            ak.stock_zh_a_hist,
            symbol=symbol,
            period="daily",
            adjust="qfq",
            timeout=config.REQUEST_TIMEOUT_SECONDS,
            **dates,
        )
    else:
        raise ValueError(f"未知历史数据源：{source}")
    if raw.empty:
        return pd.DataFrame(columns=DAILY_COLUMNS.values())
    daily = raw.rename(columns=DAILY_COLUMNS)[list(DAILY_COLUMNS.values())].copy()
    daily["symbol"] = symbol
    daily["date"] = pd.to_datetime(daily["date"], errors="coerce")
    numeric = daily.columns.difference(["symbol", "date"])
    daily[numeric] = daily[numeric].apply(pd.to_numeric, errors="coerce")
    daily = daily.replace([np.inf, -np.inf], np.nan).dropna(subset=["date", *numeric])
    valid = (daily[["open", "high", "low", "close"]] > 0).all(axis=1)
    valid &= daily["close"].between(daily["low"], daily["high"])
    valid &= daily["open"].between(daily["low"], daily["high"])
    valid &= (daily[["volume", "amount", "turnover"]] >= 0).all(axis=1)
    return daily.loc[valid].drop_duplicates(["symbol", "date"]).sort_values("date")


@lru_cache(maxsize=1)
def get_trading_days() -> tuple[date, ...]:
    calendar = request(ak.tool_trade_date_hist_sina)
    days = tuple(sorted(pd.to_datetime(calendar["trade_date"]).dt.date.unique()))
    if not days:
        raise ValueError("未能获取交易日历。")
    return days


def trading_days_through(day: date) -> list[date]:
    days = get_trading_days()
    if day > days[-1]:
        raise ValueError(f"交易日历只覆盖到 {days[-1]}，请更新 AKShare 后重试。")
    return [item for item in days if item <= day]


def update_history(
    connection, end: date, *, initialize: bool = False, source: str | None = None
) -> dict:
    """下载线程不访问数据库；更新时刷新完整本地前复权区间。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from src import database

    state = database.load_state()
    previous_source = state.get("history_source", "eastmoney")
    source = source or previous_source
    if source not in ("eastmoney", "sina"):
        raise ValueError(f"未知历史数据源：{source}")
    if (
        source != previous_source
        and connection.execute("SELECT EXISTS(SELECT 1 FROM daily_prices)").fetchone()[
            0
        ]
    ):
        raise ValueError(
            "数据库已有其他来源的日线，请使用独立数据目录，避免混合前复权口径。"
        )
    logger.info("获取股票名单，历史数据源：%s", source)
    stocks = get_stock_list(source)
    days = trading_days_through(end)
    start = days[max(0, len(days) - config.HISTORY_DAYS)]
    ranges = connection.execute(
        "SELECT symbol, min(date), max(date) FROM daily_prices GROUP BY symbol"
    ).fetchall()
    earliest = {symbol: first for symbol, first, _ in ranges}
    latest = {symbol: last for symbol, _, last in ranges}
    symbols = [
        symbol
        for symbol in stocks.symbol
        if symbol not in latest or latest[symbol] < end
    ]
    skipped = len(stocks) - len(symbols)
    logger.info(
        "目标股票 %s，只需下载 %s，已跳过 %s",
        len(stocks),
        len(symbols),
        skipped,
    )
    database.save_state(stocks=stocks.to_dict("records"), history_source=source)
    failures = {}
    saved = 0

    def download(symbol):
        try:
            history = get_daily_history(
                symbol, earliest.get(symbol, start), end, source
            )
        finally:
            if source == "sina":
                time.sleep(1)
        if symbol not in earliest:
            history = history.tail(config.HISTORY_DAYS)
        if history.empty:
            raise ValueError("未返回有效日线")
        return history

    if source == "sina":
        workers = config.SINA_DOWNLOAD_WORKERS
    else:
        workers = config.DOWNLOAD_WORKERS if initialize else 1
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(download, symbol): symbol for symbol in symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                history = future.result()
            except Exception as exc:
                failures[symbol] = str(exc)
                logger.warning("%s 日线下载失败：%s", symbol, exc, exc_info=True)
                continue
            database.save_daily(connection, history)
            saved += 1
            if saved % 100 == 0:
                logger.info("已保存 %s/%s 只股票日线", saved, len(symbols))
    result = {
        "date": end.isoformat(),
        "saved": saved,
        "failed": failures,
        "skipped": skipped,
    }
    database.save_state(update=result)
    logger.info(
        "日线更新完成：成功 %s，失败 %s，跳过 %s",
        saved,
        len(failures),
        skipped,
    )
    if symbols and not saved:
        raise ValueError("本轮没有成功下载日线，请检查行情接口后重试。")
    return result
