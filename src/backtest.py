"""日线近似验证，统计价格变化而非实际交易收益。"""

import pandas as pd

from src import config, features


def historical_signals(history, *, clv_min=None, turnover_min=None, position_max=None):
    daily = features.calculate_daily_features(history)
    grouped = daily.groupby("symbol", sort=False)
    for horizon in config.RETURN_DAYS:
        daily[f"return_{horizon}d"] = grouped.close.shift(-horizon) / daily.close - 1
    for column, output, method in [
        ("high", "mfe_20d", "max"),
        ("low", "mae_20d", "min"),
    ]:
        future = grouped[column].transform(
            lambda values, method=method: getattr(
                values.iloc[::-1].shift(1).rolling(20), method
            )().iloc[::-1]
        )
        daily[output] = future / daily.close - 1
    mask = features.low_position_mask(daily, position_max)
    mask &= daily.volume_ratio_daily >= config.VOLUME_RATIO_MIN
    if clv_min is not None:
        mask &= daily.clv > clv_min
    if turnover_min is not None:
        mask &= daily.turnover >= turnover_min
    return daily.loc[mask].reset_index(drop=True)


def summarize(signals):
    rows = []
    for horizon in config.RETURN_DAYS:
        values = signals[f"return_{horizon}d"].dropna()
        rows.append(
            {
                "交易日": horizon,
                "样本数": len(values),
                "平均收益": values.mean(),
                "收益中位数": values.median(),
                "上涨比例": (values > 0).mean(),
                "下跌比例": (values < 0).mean(),
            }
        )
    excursions = signals[["mfe_20d", "mae_20d"]].dropna()
    return pd.DataFrame(rows), {
        "信号数": len(signals),
        "完整20日样本数": len(excursions),
        "平均MFE": excursions.mfe_20d.mean(),
        "平均MAE": excursions.mae_20d.mean(),
    }
