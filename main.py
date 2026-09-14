"""命令行入口，运行前关闭页面及其他扫描进程。"""

import argparse
import json
import logging
import math
from datetime import datetime

from src import backtest, config, data_source, database, scanner

logger = logging.getLogger(__name__)


def main(argv=None):
    parser = argparse.ArgumentParser(description="A 股低位放量扫描工具")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="首次下载最近250个交易日日线")
    commands.add_parser("update", help="刷新完整本地前复权日线")
    commands.add_parser("pool", help="生成最近收盘日低位股票池")
    scan = commands.add_parser("scan", help="盘中自动扫描，Ctrl+C停止")
    scan.add_argument("--once", action="store_true", help="只扫描一次")
    test = commands.add_parser("backtest", help="日线信号与后续收益统计")
    test.add_argument("--clv-min", type=float, choices=[0.6, 0.8])
    test.add_argument("--turnover-min", type=float)
    test.add_argument("--position-max", type=float, default=config.BOTTOM_POSITION_MAX)
    args = parser.parse_args(argv)
    if args.command == "backtest" and (
        not 0 < args.position_max <= 1
        or (args.turnover_min is not None and args.turnover_min < 0)
    ):
        parser.error("价格位置必须在 (0, 1]，换手率必须非负")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    try:
        with database.connect() as connection:
            if args.command in ("init", "update"):
                day = scanner.last_completed_day(datetime.now(config.TIMEZONE))
                result = data_source.update_history(
                    connection, day, initialize=args.command == "init"
                )
                return 1 if result["failed"] else 0
            if args.command == "pool":
                print(scanner.build_pool(connection).to_string(index=False))
            elif args.command == "scan":
                if args.once:
                    metadata, rows = scanner.scan_once(connection)
                    print(metadata)
                    print(rows.to_string(index=False))
                else:
                    scanner.run_auto_scan(connection)
            elif args.command == "backtest":
                history = database.load_daily(connection)
                if history.empty:
                    raise ValueError("还没有日线数据，请先运行 init。")
                stocks = database.load_state().get("stocks", [])
                history = history.loc[
                    history.symbol.isin([row["symbol"] for row in stocks])
                ]
                signals = backtest.historical_signals(
                    history,
                    clv_min=args.clv_min,
                    turnover_min=args.turnover_min,
                    position_max=args.position_max,
                )
                summary, excursions = backtest.summarize(signals)
                output = config.DB_PATH.parent / "backtest"
                output.mkdir(exist_ok=True)
                signals.to_csv(output / "signals.csv", index=False)
                summary.to_csv(output / "summary.csv", index=False)
                (output / "excursions.json").write_text(
                    json.dumps(
                        {
                            key: None
                            if isinstance(value, float) and math.isnan(value)
                            else value
                            for key, value in excursions.items()
                        },
                        ensure_ascii=False,
                        indent=2,
                        allow_nan=False,
                    )
                )
                print(summary.to_string(index=False))
                print(excursions)
                print(
                    f"结果保存至 {output}；0.05 表示 5%。日线近似验证不等同盘中效果。"
                )
    except KeyboardInterrupt:
        logger.info("已停止扫描")
    except Exception:
        logger.exception("执行失败")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
