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

首次下载最近 250 个交易日。东方财富首次初始化最多使用 8 个下载线程，新浪使用 3 个，数据统一由主线程保存。下载失败的股票会记录到日志及 `data/market.json`；部分失败命令返回非零，可直接重跑。已有目标日期数据的股票会自动跳过，只补缺失或日期落后的股票。有效日线不足 120 条的股票不入池。

## 东方财富连接失败时

如果 `init` 报 `RemoteDisconnected / ConnectionError`，可显式使用 AKShare 的沪深交易所股票名单与新浪前复权日线：

```bash
uv run python main.py init --source sina
uv run python main.py pool
```

新浪日线成交量从股转换为手，换手率从比例转换为百分数，保持项目单位一致。新浪下载使用 3 个线程，每个线程处理一只股票后间隔 1 秒。数据源保存在 `data/market.json`，后续 `update` 和重跑 `init` 自动沿用，无需重复指定。部分失败可重跑，已存在日期会覆盖而不会重复插入。

只有空数据库可更换来源；已有日线时拒绝切换，避免混合不同前复权口径。首次初始化失败留下的空数据库可以直接使用上述命令。已有数据要换源时，应先关闭程序，另行备份并移走整个 `data/` 目录后重新初始化。

这个选项解决历史数据获取，盘中扫描的实时量比仍来自东方财富；历史下载成功不表示实时接口已恢复。新浪接口同样可能出现连接或限流问题，不自动切换来源。字段单位依据 [AKShare 新浪日线文档](https://akshare.akfamily.xyz/data/stock/stock.html)。

## 每日使用

收盘后关闭页面和其他扫描程序，再执行：

```bash
uv run python main.py update
uv run python main.py pool
```

更新会跳过已有目标日期数据的股票。日期落后的股票只下载本地最后日期之后的数据；新股票下载最近 250 个交易日。同一天重跑时只补缺失或仍然落后的股票。更新失败或停牌导致缺少当日数据的股票不参与当日股票池。

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
