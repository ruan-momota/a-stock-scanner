"""获取和整理行情数据，不包含选股规则。"""

import csv
import logging
import re
import time
from datetime import date, datetime
from functools import lru_cache

import akshare as ak
import numpy as np
import pandas as pd
import requests

from src import config

logger = logging.getLogger(__name__)

REALTIME_COLUMNS = [
    "symbol",
    "name",
    "price",
    "change_pct",
    "volume",
    "amount",
    "day_high",
    "day_low",
    "volume_ratio",
    "turnover",
]
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
SINA_QUOTE_URL = "https://hq.sinajs.cn/"
SINA_QUOTE_HEADERS = {
    "Referer": "https://finance.sina.com.cn/",
    "User-Agent": "Mozilla/5.0",
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


def _parse_sina_quotes(text: str) -> pd.DataFrame:
    rows = []
    for code, payload in re.findall(r'var hq_str_(\w+)="(.*?)";', text):
        values = next(csv.reader([payload]))
        if len(values) < 32 or not values[0]:
            continue
        rows.append(
            {
                "symbol": code[-6:],
                "name": values[0],
                "previous_close": values[2],
                "price": values[3],
                "day_high": values[4],
                "day_low": values[5],
                "volume": values[8],
                "amount": values[9],
                "quote_date": values[30],
            }
        )
    return pd.DataFrame(rows)


def _get_sina_quote_batch(symbols: list[str]) -> pd.DataFrame:
    codes = [("sh" if symbol.startswith("6") else "sz") + symbol for symbol in symbols]

    def download():
        response = requests.get(
            f"{SINA_QUOTE_URL}?list={','.join(codes)}",
            headers=SINA_QUOTE_HEADERS,
            timeout=config.REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.content.decode("gb18030", errors="replace")

    quotes = _parse_sina_quotes(request(download))
    if quotes.empty:
        raise ValueError("新浪本批实时行情为空，请稍后重试。")
    return quotes


def _trading_fraction(now: datetime) -> float:
    minutes = now.hour * 60 + now.minute + now.second / 60
    morning = min(max(minutes - 570, 0), 120)
    afternoon = min(max(minutes - 780, 0), 120)
    return max((morning + afternoon) / 240, 1 / 240)


def get_realtime_quotes(
    symbols: list[str], baselines: pd.DataFrame, now: datetime
) -> pd.DataFrame:
    """只获取股票池的新浪报价，并补充盘中估算指标。"""
    symbols = list(dict.fromkeys(str(symbol).zfill(6) for symbol in symbols))
    if not symbols:
        return pd.DataFrame(columns=REALTIME_COLUMNS)
    frames = [
        _get_sina_quote_batch(symbols[index : index + config.SINA_QUOTE_BATCH_SIZE])
        for index in range(0, len(symbols), config.SINA_QUOTE_BATCH_SIZE)
    ]
    quotes = pd.concat(frames, ignore_index=True)
    if quotes.empty:
        raise ValueError("新浪实时行情为空，请稍后重试。")
    quote_days = pd.to_datetime(quotes["quote_date"], errors="coerce").dt.date
    quotes = quotes.loc[quote_days == now.date()].copy()
    if quotes.empty:
        raise ValueError("新浪未返回当天实时行情，请稍后重试。")
    numeric = [
        "previous_close",
        "price",
        "day_high",
        "day_low",
        "volume",
        "amount",
    ]
    quotes[numeric] = quotes[numeric].apply(pd.to_numeric, errors="coerce")
    quotes["volume"] /= 100
    quotes["change_pct"] = (
        quotes["price"] / quotes["previous_close"].where(quotes.previous_close > 0) - 1
    ) * 100
    quotes = quotes.merge(baselines, on="symbol", how="left")
    quotes["volume_ratio"] = quotes["volume"] / (
        quotes["avg_volume_20"].where(quotes.avg_volume_20 > 0) * _trading_fraction(now)
    )
    quotes["turnover"] = quotes["volume"] / quotes["volume_per_turnover"].where(
        quotes.volume_per_turnover > 0
    )
    quotes = quotes.reindex(columns=REALTIME_COLUMNS)
    logger.info(
        "新浪批量行情获取成功，请求股票池 %s 只，返回 %s 只",
        len(symbols),
        len(quotes),
    )
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


def get_stock_list(source: str = "sina") -> pd.DataFrame:
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
            raw = request(ak.stock_zh_a_spot_em)
            if raw.empty or not {"代码", "名称"}.issubset(raw.columns):
                raise ValueError("股票名单为空或缺少代码、名称。")
            stocks = raw.rename(columns={"代码": "symbol", "名称": "name"})[
                ["symbol", "name"]
            ]
            stocks["symbol"] = stocks["symbol"].astype(str).str.zfill(6)
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
    symbol: str, start: date, end: date, source: str = "sina"
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
    return clean_daily(daily)


def clean_daily(daily: pd.DataFrame) -> pd.DataFrame:
    daily = daily[list(DAILY_COLUMNS.values())].copy()
    daily["date"] = pd.to_datetime(daily["date"], errors="coerce")
    numeric = daily.columns.difference(["symbol", "date"])
    daily[numeric] = daily[numeric].apply(pd.to_numeric, errors="coerce")
    required = ["open", "high", "low", "close", "volume", "amount"]
    daily = daily.replace([np.inf, -np.inf], np.nan).dropna(subset=["date", *required])
    valid = (daily[["open", "high", "low", "close"]] > 0).all(axis=1)
    valid &= daily["close"].between(daily["low"], daily["high"])
    valid &= daily["open"].between(daily["low"], daily["high"])
    valid &= (daily[["volume", "amount"]] >= 0).all(axis=1)
    valid &= daily["turnover"].isna() | (daily["turnover"] >= 0)
    return daily.loc[valid].drop_duplicates(["symbol", "date"]).sort_values("date")


def get_daily_snapshot(day: date) -> pd.DataFrame:
    raw = request(ak.stock_zh_a_spot)
    required = {"代码", "今开", "最高", "最低", "最新价", "成交量", "成交额"}
    if raw.empty or not required.issubset(raw.columns):
        raise ValueError("新浪全市场快照为空或缺少关键字段，请稍后重试。")
    daily = raw.rename(
        columns={
            "代码": "symbol",
            "今开": "open",
            "最高": "high",
            "最低": "low",
            "最新价": "close",
            "成交量": "volume",
            "成交额": "amount",
        }
    )
    daily["symbol"] = daily["symbol"].astype(str).str[-6:]
    daily["volume"] = pd.to_numeric(daily["volume"], errors="coerce") / 100
    daily["turnover"] = np.nan
    daily["date"] = pd.Timestamp(day)
    return clean_daily(daily)


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
    """初始化历史数据，或用新浪全市场快照追加一个交易日。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from src import database

    state = database.load_state()
    previous_source = state.get("history_source", "sina")
    source = source or previous_source
    if source not in ("eastmoney", "sina"):
        raise ValueError(f"未知历史数据源：{source}")
    has_prices = connection.execute(
        "SELECT EXISTS(SELECT 1 FROM daily_prices)"
    ).fetchone()[0]
    if source != previous_source and has_prices:
        raise ValueError(
            "数据库已有其他来源的日线，请使用独立数据目录，避免混合前复权口径。"
        )

    if not initialize:
        stocks = state.get("stocks", [])
        if not stocks or not has_prices:
            raise ValueError("还没有历史数据，请先运行 init。")
        symbols = {row["symbol"] for row in stocks}
        current = {
            row[0]
            for row in connection.execute(
                "SELECT symbol FROM daily_prices WHERE date = ?", [end]
            ).fetchall()
        }
        pending = symbols - current
        if pending:
            daily = get_daily_snapshot(end)
            daily = daily.loc[daily.symbol.isin(pending)]
            database.save_daily(connection, daily)
        else:
            daily = pd.DataFrame()
        saved_symbols = set(daily.symbol) if not daily.empty else set()
        failures = {
            symbol: "全市场快照未返回有效行情"
            for symbol in sorted(pending - saved_symbols)
        }
        result = {
            "date": end.isoformat(),
            "saved": len(saved_symbols),
            "failed": failures,
            "skipped": len(current & symbols),
        }
        database.save_state(update=result)
        logger.info(
            "快照更新完成：新增 %s，缺失 %s，已存在 %s",
            result["saved"],
            len(failures),
            result["skipped"],
        )
        return result

    logger.info("获取股票名单，历史数据源：%s", source)
    stocks = get_stock_list(source)
    days = trading_days_through(end)
    start = days[max(0, len(days) - config.HISTORY_DAYS)]
    ranges = connection.execute(
        "SELECT symbol, max(date) FROM daily_prices GROUP BY symbol"
    ).fetchall()
    latest = dict(ranges)
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
            history = get_daily_history(symbol, start, end, source)
        finally:
            if source == "sina":
                time.sleep(1)
        history = history.tail(config.HISTORY_DAYS)
        if history.empty:
            raise ValueError("未返回有效日线")
        return history

    if source == "sina":
        workers = config.SINA_DOWNLOAD_WORKERS
    else:
        workers = config.DOWNLOAD_WORKERS
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
