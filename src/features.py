"""计划中的公式。日线计算与实时扫描共用这些函数。"""

import numpy as np
import pandas as pd

from src import config


def bottom_score(position: pd.Series) -> pd.Series:
    return ((config.BOTTOM_POSITION_MAX - position) / config.BOTTOM_POSITION_MAX).clip(
        0, 1
    )


def calculate_clv(price: pd.Series, low: pd.Series, high: pd.Series) -> pd.Series:
    spread = (high - low).where(high > low)
    return ((price - low) / spread).where(price.between(low, high))


def calculate_daily_features(history: pd.DataFrame) -> pd.DataFrame:
    daily = (
        history.sort_values(["symbol", "date"])
        .drop_duplicates(["symbol", "date"])
        .copy()
    )
    numeric = ["open", "high", "low", "close", "volume", "amount", "turnover"]
    daily[numeric] = daily[numeric].replace([np.inf, -np.inf], np.nan)
    invalid = (daily[["open", "high", "low", "close"]] <= 0).any(axis=1)
    invalid |= ~daily["close"].between(daily["low"], daily["high"])
    daily.loc[invalid, ["high", "low", "close"]] = np.nan
    grouped = daily.groupby("symbol", sort=False)
    high_120 = grouped["high"].transform(lambda values: values.rolling(120).max())
    low_120 = grouped["low"].transform(lambda values: values.rolling(120).min())
    low_60 = grouped["low"].transform(lambda values: values.rolling(60).min())
    spread = (high_120 - low_120).where(high_120 > low_120)
    daily["position_120"] = (daily["close"] - low_120) / spread
    daily["drawdown_120"] = daily["close"] / high_120.where(high_120 > 0) - 1
    daily["distance_low_60"] = daily["close"] / low_60.where(low_60 > 0) - 1
    daily["bottom_score"] = bottom_score(daily["position_120"])
    mean_volume = grouped["volume"].transform(
        lambda values: values.shift(1).rolling(20).mean()
    )
    daily["volume_ratio_daily"] = daily["volume"] / mean_volume.where(mean_volume > 0)
    daily["clv"] = calculate_clv(daily["close"], daily["low"], daily["high"])
    return daily


def low_position_mask(
    daily: pd.DataFrame, position_max: float | None = None
) -> pd.Series:
    maximum = config.BOTTOM_POSITION_MAX if position_max is None else position_max
    return (
        daily["position_120"].between(0, maximum)
        & (daily["drawdown_120"] <= config.DRAWDOWN_MIN)
        & daily["distance_low_60"].between(0, config.DISTANCE_FROM_LOW_MAX)
    )


def calculate_scores(candidates: pd.DataFrame) -> pd.DataFrame:
    rows = candidates.copy().replace([np.inf, -np.inf], np.nan)
    rows["clv"] = calculate_clv(rows["price"], rows["day_low"], rows["day_high"])
    rows = rows.dropna(
        subset=["price", "amount", "volume_ratio", "turnover", "bottom_score", "clv"]
    )
    rows = rows.loc[(rows["price"] > 0) & (rows["turnover"] >= 0)]
    volume_score = ((rows["volume_ratio"] - 1) / 3).clip(0, 1)
    turnover_score = (rows["turnover"] / 5).clip(0, 1)
    rows["priority_score"] = (
        rows["bottom_score"] * 0.35
        + volume_score * 0.35
        + rows["clv"] * 0.20
        + turnover_score * 0.10
    ) * 100
    return rows.sort_values("priority_score", ascending=False).reset_index(drop=True)
