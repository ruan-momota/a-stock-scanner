# A 股低位放量扫描

个人选股辅助工具：收盘日线生成低位股票池，盘中按量比、成交额筛选并评分，保存所有候选。规则和范围见 [第一版计划](docs/plan-v1.md)。

## 安装与首次运行

需要 Python 3.12 和 uv：

```bash
uv sync
uv run python main.py init
uv run python main.py pool
uv run streamlit run app.py
```

首次下载最近 250 个交易日，最多 8 个下载线程，由主线程保存。下载失败的股票会记录到日志及 `data/market.json`；部分失败命令返回非零，可再次运行恢复。有效日线不足 120 条的股票不入池。

## 每日使用

收盘后关闭页面和其他扫描程序，再执行：

```bash
uv run python main.py update
uv run python main.py pool
```

更新会从每只股票本地最早日期重新下载前复权日线，覆盖同日期数据。更新失败或停牌导致缺少当日数据的股票不参与当日股票池。

页面默认手动，点击“扫描一次”；选择“自动”后在交易时段每 60 秒扫描，切回手动停止定时扫描。查看详情时继续自动扫描，关闭浏览器会话停止。非交易时段显示已有结果，不新增盘中信号。显示过滤不改变已保存记录。

也可以关闭页面，仅使用命令行：

```bash
uv run python main.py scan --once
uv run python main.py scan
```

自动扫描在开盘前及午休等待，收盘或非交易日结束，Ctrl+C 停止。所有交易时间按 Asia/Shanghai。数据库采用单进程访问，页面、命令行扫描、更新及回测不要同时运行。

## 历史验证

```bash
uv run python main.py backtest
uv run python main.py backtest --clv-min 0.6
uv run python main.py backtest --clv-min 0.8
uv run python main.py backtest --turnover-min 5
uv run python main.py backtest --position-max 0.2
```

输出 5/10/20/60 个交易日的样本数、均值、中位数、上涨和下跌比例，以及未来完整 20 日的平均 MFE/MAE。未来数据不足的样本按期限排除。结果保存在 `data/backtest/`，每次运行覆盖上次导出，比较时请另行保存。

日线量比使用此前 20 日均量，不包含信号当天；MFE/MAE 从次日起计算。该验证用收盘条件近似，不还原盘中量比，不代表实际交易收益。当前股票范围和短历史存在样本局限。真实盘中候选从首次扫描起持续存入 `scan_results`，几个月后的实际跟踪效果仍需积累数据后分析。

## 检查

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

主要文件：`main.py` 命令行，`app.py` 页面，`src/data_source.py` 行情与日线更新，`database.py` 存储，`features.py` 指标，`scanner.py` 扫描，`backtest.py` 历史统计，`config.py` 参数。四张表保存在 `data/market.duckdb`，旁边 JSON 保存股票名单、股票池日期和最近扫描状态。`signals` 预留人工关注记录，第一版不增加关注按钮。
