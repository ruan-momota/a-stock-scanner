import numpy as np
import pandas as pd
import pytest

from src import backtest, features


def history(length=180):
    return pd.DataFrame(
        {
            "symbol": "600000",
            "date": pd.bdate_range("2025-01-01", periods=length),
            "open": 11.0,
            "high": 20.0,
            "low": 10.0,
            "close": 11.0,
            "volume": 100.0,
            "amount": 100000000.0,
            "turnover": 2.0,
        }
    )


def test_bottom_features_and_insufficient_history():
    daily = features.calculate_daily_features(history())
    assert pd.isna(daily.iloc[118].position_120)
    row = daily.iloc[119]
    assert row.position_120 == pytest.approx(0.1)
    assert row.drawdown_120 == pytest.approx(-0.45)
    assert row.distance_low_60 == pytest.approx(0.1)
    assert row.bottom_score == pytest.approx(2 / 3)
    assert features.low_position_mask(daily).iloc[119]
    assert not features.low_position_mask(daily).iloc[118]


def test_clv_invalid_and_scores():
    values = features.calculate_clv(
        pd.Series([11.0, 10.0, 25.0]),
        pd.Series([10.0, 10.0, 10.0]),
        pd.Series([20.0, 10.0, 20.0]),
    )
    assert values.iloc[0] == pytest.approx(0.1)
    assert values.iloc[1:].isna().all()
    candidates = pd.DataFrame(
        {
            "price": [15.0, 15.0, np.nan],
            "day_low": 10.0,
            "day_high": 20.0,
            "bottom_score": 1.0,
            "volume_ratio": [4.0, 1.0, 4.0],
            "turnover": 5.0,
            "amount": 100000000.0,
        }
    )
    rows = features.calculate_scores(candidates)
    assert rows.priority_score.tolist() == pytest.approx([90.0, 55.0])
    assert features.bottom_score(pd.Series([-0.1, 0.3, 0.5])).tolist() == [
        1.0,
        0.0,
        0.0,
    ]


def test_zero_range_and_missing_data():
    data = history()
    data[["open", "high", "low", "close"]] = 10.0
    assert features.calculate_daily_features(data).position_120.isna().all()
    data = history()
    data.loc[50, "high"] = np.nan
    assert pd.isna(features.calculate_daily_features(data).iloc[119].position_120)


def test_backtest_excludes_signal_day_and_incomplete_future():
    data = history()
    data.loc[119, "volume"] = 200.0
    data.loc[124, "close"] = 12.0
    data.loc[120:139, "high"] = 15.0
    data.loc[179, "volume"] = 200.0
    signals = backtest.historical_signals(data)
    first = signals.iloc[0]
    assert first.volume_ratio_daily == 2.0
    assert first.return_5d == pytest.approx(12 / 11 - 1)
    assert first.mfe_20d == pytest.approx(15 / 11 - 1)
    assert first.mae_20d == pytest.approx(10 / 11 - 1)
    assert pd.isna(signals.iloc[-1].return_5d)
    summary, stats = backtest.summarize(signals)
    assert summary.iloc[0]["样本数"] == 1
    assert stats["完整20日样本数"] == 1
    altered = data.copy()
    altered.loc[120:, "volume"] = 1e9
    assert backtest.historical_signals(altered).iloc[0].volume_ratio_daily == 2.0
