"""个人使用的实时扫描及个股详情页面。"""

import logging
import time
from datetime import datetime

import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from src import config, database, scanner

logger = logging.getLogger(__name__)

st.set_page_config(page_title="A 股低位放量扫描", layout="wide")
st.title("A 股低位放量扫描")
st.caption("低位、放量与日内价格位置帮助确定查看顺序，评分不代表收益预测。")
mode = st.sidebar.radio("扫描模式", ["手动", "自动"])
if st.session_state.get("previous_mode") != mode:
    st.session_state.next_scan = 0.0
    st.session_state.previous_mode = mode


def show_detail(connection, row):
    st.subheader(f"{row.symbol} · {row['name']}")
    columns = st.columns(6)
    for column, label, value in zip(
        columns,
        ["最新价", "涨跌幅", "量比", "换手率", "成交额", "CLV"],
        [
            f"{row.price:.2f}",
            f"{row.change_pct:.2f}%",
            f"{row.volume_ratio:.2f}",
            f"{row.turnover:.2f}%",
            f"{row.amount / 1e8:.2f} 亿元",
            f"{row.clv:.2f}",
        ],
    ):
        column.metric(label, value)
    st.write(
        f"位于 120 日价格区间底部 {row.position_120:.1%}；"
        f"比 120 日高点低 {-row.drawdown_120:.1%}；"
        f"距 60 日低点上涨 {row.distance_low_60:.1%}；量比 {row.volume_ratio:.2f}。"
    )
    history = database.load_daily(connection, row.symbol)
    if history.empty:
        st.info("暂无历史日线。")
        return
    for window in (20, 60):
        history[f"MA{window}"] = history.close.rolling(window).mean()
    history = history.tail(120)
    figure = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        row_heights=[0.75, 0.25],
        vertical_spacing=0.04,
    )
    figure.add_trace(
        go.Candlestick(
            x=history.date,
            open=history.open,
            high=history.high,
            low=history.low,
            close=history.close,
            name="前复权日线",
            increasing_line_color="#d64b4b",
            decreasing_line_color="#249867",
        ),
        row=1,
        col=1,
    )
    for window in (20, 60):
        figure.add_trace(
            go.Scatter(x=history.date, y=history[f"MA{window}"], name=f"MA{window}"),
            row=1,
            col=1,
        )
    figure.add_trace(
        go.Bar(x=history.date, y=history.volume, name="成交量（手）"), row=2, col=1
    )
    figure.update_layout(
        height=650,
        xaxis_rangeslider_visible=False,
        margin={"l": 10, "r": 10, "t": 20, "b": 10},
    )
    st.plotly_chart(figure, width="stretch")


@st.fragment(run_every=config.SCAN_INTERVAL_SECONDS if mode == "自动" else None)
def dashboard():
    clicked = st.button("扫描一次", disabled=mode == "自动")
    due = mode == "自动" and time.monotonic() >= st.session_state.get("next_scan", 0)
    try:
        with database.connect() as connection:
            phase = "手动待命"
            if clicked or due:
                st.session_state.next_scan = (
                    time.monotonic() + config.SCAN_INTERVAL_SECONDS
                )
                try:
                    now = datetime.now(config.TIMEZONE)
                    phase = scanner.market_phase(now)
                    scanner.scan_once(connection, now)
                    st.session_state.scan_error = None
                    st.session_state.phase = phase
                except Exception as exc:
                    logger.exception("页面扫描失败")
                    st.session_state.scan_error = str(exc)
            if st.session_state.get("scan_error"):
                st.error(
                    f"本轮扫描失败：{st.session_state.scan_error}。保留上次成功结果。"
                )
            metadata, rows = database.load_latest_scan(connection)
            st.caption(
                f"模式：{mode} · 状态：{st.session_state.get('phase', phase)} · "
                f"上次成功扫描：{metadata.get('timestamp', '尚未扫描')}（上海时间）"
            )
            if not database.load_state().get("pool_date"):
                st.info("请先在终端运行 init 和 pool，再打开页面扫描。")
            filters = st.columns(4)
            ratio = filters[0].number_input(
                "最低量比",
                min_value=config.VOLUME_RATIO_MIN,
                value=config.VOLUME_RATIO_MIN,
                step=0.1,
            )
            amount = filters[1].number_input(
                "最低成交额（万元）",
                min_value=config.MIN_AMOUNT / 10000,
                value=config.MIN_AMOUNT / 10000,
                step=1000.0,
            )
            position = filters[2].number_input(
                "最大120日价格位置",
                min_value=0.0,
                max_value=config.BOTTOM_POSITION_MAX,
                value=config.BOTTOM_POSITION_MAX,
                step=0.01,
            )
            score = filters[3].number_input(
                "最低评分", min_value=0.0, max_value=100.0, value=0.0
            )
            shown = rows.loc[
                (rows.volume_ratio >= ratio)
                & (rows.amount >= amount * 10000)
                & (rows.position_120 <= position)
                & (rows.priority_score >= score)
            ]
            metrics = st.columns(3)
            pool_size = metadata.get("pool_size", len(database.load_pool(connection)))
            metrics[0].metric("低位股票池", pool_size)
            metrics[1].metric("扫描候选", metadata.get("candidate_count", 0))
            metrics[2].metric("显示条件内候选", len(shown))
            count = st.radio("显示数量", [50, 20], horizontal=True)
            shown = shown.head(count).reset_index(drop=True)
            if shown.empty:
                st.info("暂无符合显示条件的候选。")
                return
            columns = [
                "symbol",
                "name",
                "price",
                "change_pct",
                "volume_ratio",
                "amount",
                "turnover",
                "position_120",
                "clv",
                "priority_score",
            ]
            labels = [
                "代码",
                "名称",
                "最新价",
                "涨跌幅(%)",
                "量比",
                "成交额(元)",
                "换手率(%)",
                "120日价格位置",
                "CLV",
                "查看优先级评分",
            ]
            event = st.dataframe(
                shown[columns],
                hide_index=True,
                on_select="rerun",
                selection_mode="single-row",
                column_config=dict(zip(columns, labels)),
                key=f"results_{metadata.get('timestamp')}",
            )
            if event.selection.rows:
                st.session_state.detail_symbol = shown.iloc[
                    event.selection.rows[0]
                ].symbol
            symbols = rows.symbol.tolist()
            selected = st.session_state.get("detail_symbol")
            options = [None, *symbols]
            symbol = st.selectbox(
                "个股详情",
                options,
                index=options.index(selected) if selected in symbols else 0,
                format_func=lambda value: (
                    "选择股票或点击上方表格" if value is None else value
                ),
            )
            if symbol is not None:
                show_detail(connection, rows.loc[rows.symbol == symbol].iloc[0])
    except Exception as exc:
        logger.exception("页面读取失败")
        st.error(f"无法读取数据：{exc}。请关闭其他命令行或页面进程后重试。")


dashboard()
