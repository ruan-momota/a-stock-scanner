import logging
import time
from datetime import date, datetime, timedelta
from datetime import time as clock_time

from src import config, data_source, database, features

logger = logging.getLogger(__name__)


def market_phase(now: datetime) -> str:
    now = now.astimezone(config.TIMEZONE)
    clock = now.time()
    if now.weekday() >= 5:
        return "非交易日"
    days = data_source.trading_days_through(now.date())
    if days[-1] != now.date():
        return "非交易日"
    if clock >= clock_time(15):
        return "已收盘"
    if clock < clock_time(9, 30):
        return "等待开盘"
    if clock_time(11, 30) <= clock < clock_time(13):
        return "午休"
    return "交易中"


def last_completed_day(now: datetime) -> date:
    now = now.astimezone(config.TIMEZONE)
    cutoff = (
        now.date() if now.time() >= clock_time(15) else now.date() - timedelta(days=1)
    )
    return data_source.trading_days_through(cutoff)[-1]


def build_pool(connection, day: date | None = None):
    day = day or last_completed_day(datetime.now(config.TIMEZONE))
    state = database.load_state()
    if not state.get("stocks"):
        raise ValueError("还没有股票名单，请先运行 init。")
    history = database.load_daily(connection)
    if history.empty:
        raise ValueError("还没有日线数据，请先运行 init。")
    history = history.loc[history["date"] <= str(day)]
    daily = features.calculate_daily_features(history)
    pool = daily.loc[
        (daily["date"] == str(day)) & features.low_position_mask(daily)
    ].copy()
    import pandas as pd

    stocks = pd.DataFrame(state["stocks"])
    pool = pool.merge(stocks[["symbol", "name"]], on="symbol")
    database.save_pool(connection, pool, day)
    logger.info("已生成 %s 股票池，共 %s 只", day, len(pool))
    return pool


def scan_once(connection, now: datetime | None = None):
    now = (now or datetime.now(config.TIMEZONE)).astimezone(config.TIMEZONE)
    phase = market_phase(now)
    if phase != "交易中":
        logger.info("%s，显示上次扫描结果", phase)
        return database.load_latest_scan(connection)
    pool_date = data_source.trading_days_through(now.date() - timedelta(days=1))[-1]
    if database.load_state().get("pool_date") != pool_date.isoformat():
        raise ValueError(f"缺少 {pool_date} 股票池，请先运行 update 和 pool。")
    pool = database.load_pool(connection, pool_date)
    if pool.empty:
        candidates = database.load_latest_scan(connection)[1].iloc[:0]
    else:
        realtime = data_source.eligible_stocks(data_source.get_realtime_quotes())
        candidates = realtime.merge(pool.drop(columns="name"), on="symbol", how="inner")
        candidates = candidates.loc[
            (candidates["amount"] >= config.MIN_AMOUNT)
            & (candidates["volume_ratio"] >= config.VOLUME_RATIO_MIN)
        ]
        candidates = features.calculate_scores(candidates)
    metadata = database.save_scan(
        connection, candidates, now.replace(tzinfo=None), pool_date, len(pool)
    )
    logger.info("股票池 %s 只，候选 %s 只，扫描结果已保存", len(pool), len(candidates))
    return metadata, candidates


def run_auto_scan(connection):
    logger.info(
        "自动扫描已启动，每 %s 秒一轮，Ctrl+C 停止", config.SCAN_INTERVAL_SECONDS
    )
    while True:
        started = time.monotonic()
        try:
            now = datetime.now(config.TIMEZONE)
            phase = market_phase(now)
            if phase in ("非交易日", "已收盘"):
                logger.info("%s，结束扫描", phase)
                return
            if phase == "交易中":
                scan_once(connection, now)
            else:
                logger.info("%s", phase)
        except Exception:
            logger.exception("本轮扫描失败，下一轮重试")
        elapsed = time.monotonic() - started
        time.sleep(max(1, config.SCAN_INTERVAL_SECONDS - elapsed))
