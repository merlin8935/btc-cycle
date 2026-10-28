#!/usr/bin/env python3
"""爆仓潮之后有没有大反弹？事件研究（BTCUSDT 日线，2022-01 → 最新）

爆仓历史拿不到 4 年（Binance Vision 无快照，项目强平流只从 2026-05 起），
所以用「24h OI 骤降 + 当日大跌」当多头清洗事件的代理，并用 2026-05 之后的真实强平流验证代理。

用法：~/oi-data/.venv/bin/python liq_rebound_study.py [--oi -4] [--ret -2] [--target 10]
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, "/Users/merlin/oi-data")
from archive import get_klines, get_metrics  # noqa: E402

HERE = Path(__file__).resolve().parent
DB = "/Users/merlin/oi-data/data/flows.db"
START, END = "2022-01-01", "2026-10-26"
FNG_CACHE = HERE / "fng_history.json"


def rsi(s, n=14):
    d = s.diff()
    up, dn = d.clip(lower=0), -d.clip(upper=0)
    rs = up.ewm(alpha=1 / n, adjust=False).mean() / dn.ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + rs)


def load_daily():
    k = get_klines("BTCUSDT", "1d", START, END)
    k["date"] = k["open_time_dt"].dt.strftime("%Y-%m-%d")
    k = k.set_index("date")[["open", "high", "low", "close", "volume", "taker_buy_volume"]].astype(float)

    m = get_metrics("BTCUSDT", START, END)
    m["date"] = m["create_time"].str[:10]
    cols = ["sum_open_interest", "sum_open_interest_value", "count_toptrader_long_short_ratio",
            "sum_toptrader_long_short_ratio", "count_long_short_ratio", "sum_taker_long_short_vol_ratio"]
    m[cols] = m[cols].astype(float)
    m = m.sort_values("create_time").reset_index(drop=True)
    md = m.groupby("date")[cols].last()  # 每日最后一笔 5m 快照
    md["taker_ratio_day"] = m.groupby("date")["sum_taker_long_short_vol_ratio"].mean()
    # 日内清洗强度：滚动 4h（48 根 5m）OI 变化的当日最小值；0/缺失快照会造成 -100% 假值，截掉
    oi = m["sum_open_interest"].where(m["sum_open_interest"] > 0)
    m["oi4"] = ((oi / oi.shift(48) - 1) * 100).clip(lower=-50)
    md["min_oi_4h"] = m.groupby("date")["oi4"].min()

    con = sqlite3.connect(DB)
    fr = pd.read_sql("select ts, rate from funding_rate where exchange='binance' and symbol='BTCUSDT'", con)
    fr["date"] = pd.to_datetime(fr["ts"], unit="ms", utc=True).dt.strftime("%Y-%m-%d")
    fd = fr.groupby("date")["rate"].mean() * 100  # 当日 3 次结算平均（%）
    oc = pd.read_sql("select date, metric, value from onchain_metrics where metric='mvrv_zscore'", con)
    mv = oc.set_index("date")["value"]
    con.close()

    if not FNG_CACHE.exists():
        j = requests.get("https://api.alternative.me/fng/?limit=0&format=json", timeout=30).json()["data"]
        FNG_CACHE.write_text(json.dumps(j))
    fng = pd.DataFrame(json.loads(FNG_CACHE.read_text()))
    fng["date"] = pd.to_datetime(fng["timestamp"].astype(int), unit="s", utc=True).dt.strftime("%Y-%m-%d")
    fs = fng.set_index("date")["value"].astype(float)

    df = k.join(md, how="left").join(fd.rename("funding"), how="left").join(mv.rename("mvrv_z"), how="left")
    df = df.join(fs.rename("fng"), how="left")
    df["oi"] = df["sum_open_interest"]
    df["ret1"] = df["close"].pct_change() * 100
    df["doi1"] = df["oi"].pct_change() * 100
    df["doi7"] = df["oi"].pct_change(7) * 100
    df["ret7"] = df["close"].pct_change(7) * 100
    df["ret30"] = df["close"].pct_change(30) * 100
    df["rsi14"] = rsi(df["close"])
    df["ma200_dist"] = (df["close"] / df["close"].rolling(200).mean() - 1) * 100
    df["ma50_dist"] = (df["close"] / df["close"].rolling(50).mean() - 1) * 100
    df["ma140_dist"] = (df["close"] / df["close"].rolling(140).mean() - 1) * 100  # ≈ 20 周均线
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    df["lower_wick"] = (df[["open", "close"]].min(axis=1) - df["low"]) / rng * 100
    df["vol_z"] = (df["volume"] - df["volume"].rolling(20).mean()) / df["volume"].rolling(20).std()
    df["low30_dist"] = (df["close"] / df["low"].rolling(30).min() - 1) * 100
    df["range"] = (df["high"] / df["low"] - 1) * 100
    df["down_wick"] = (df[["open", "close"]].min(axis=1) / df["low"] - 1) * 100
    df["top_ls"] = df["count_toptrader_long_short_ratio"]
    df["global_ls"] = df["count_long_short_ratio"]
    df["taker"] = df["taker_ratio_day"]
    return df


def find_events(df, oi_thr, ret_thr, cooldown=5):
    flag = (df["min_oi_4h"] <= oi_thr) & (df["ret1"] <= ret_thr)
    # 连环爆：事件前 14 天（不含当天）里已出现几次清洗日
    df["n_prev14"] = flag.astype(int).rolling(14).sum().shift(1)
    events, last = [], None
    dates = list(df.index)
    for i, d in enumerate(dates):
        if not flag.iloc[i]:
            continue
        if last is not None and i - last < cooldown:
            continue
        events.append(i)
        last = i
    return events


def forward(df, i, horizon):
    c0 = df["close"].iloc[i]
    fut = df["close"].iloc[i + 1:i + 1 + horizon]
    lows = df["low"].iloc[i + 1:i + 1 + horizon]
    if len(fut) < horizon:
        return None
    mfe_h = {f"mfe{h}": (df["close"].iloc[i + 1:i + 1 + h].max() / c0 - 1) * 100 if i + h < len(df) else np.nan
             for h in (30, 60)}
    return dict(mfe=(fut.max() / c0 - 1) * 100, mae=(lows.min() / c0 - 1) * 100,
                days_to_max=int(fut.values.argmax()) + 1, **mfe_h,
                r7=(df["close"].iloc[i + 7] / c0 - 1) * 100 if i + 7 < len(df) else np.nan,
                r14=(df["close"].iloc[i + 14] / c0 - 1) * 100 if i + 14 < len(df) else np.nan,
                r30=(df["close"].iloc[i + 30] / c0 - 1) * 100 if i + 30 < len(df) else np.nan,
                next_up=df["close"].iloc[i + 1] > c0)


def validate_proxy(df):
    """2026-05-26 起的真实强平流：每日多头被强平美元 vs 当日 OI 变化"""
    con = sqlite3.connect(DB)
    q = ("select ts, side, notional_usdt from flows_liquidations where symbol in ('BTCUSDT','BTCUSDT.P','BTCUSDT_PERP') "
         "or symbol like 'BTCUSDT%'")
    liq = pd.read_sql(q, con)
    con.close()
    liq["date"] = pd.to_datetime(liq["ts"], unit="ms", utc=True).dt.strftime("%Y-%m-%d")
    # 注意：ws_liquidations.py 对 Bybit 的 side 映射是反的（S=Buy 才是多头被平，脚本记成 short），
    # 2026-08-19 +7% 大涨日库里"long" $240M 即证据。这里按库里的反向取：side='short' = 真实多头被平
    long_side = liq["side"] == "short"
    ld = liq[long_side].groupby("date")["notional_usdt"].sum() / 1e6
    j = df[["doi1", "min_oi_4h", "ret1", "range"]].join(ld.rename("long_liq_m"), how="inner").dropna()
    j = j[j.index > "2026-05-26"]
    rho = {c: j["long_liq_m"].rank().corr(j[c].rank()) for c in ("doi1", "min_oi_4h", "ret1", "range")}
    top = j.sort_values("long_liq_m", ascending=False).head(8)
    return rho, top, j


FEATURES = [
    ("funding", "当日资金费率 %(8h 均)", "低"), ("fng", "恐贪指数", "低"), ("rsi14", "RSI14(日)", "低"),
    ("ma200_dist", "距 MA200 %", "低"), ("ma140_dist", "距 20 周均线 %", "低"), ("ret7", "前 7 日涨跌 %", "低"),
    ("ret30", "前 30 日涨跌 %", "低"), ("doi7", "OI 7 日变化 %", "低"), ("doi1", "当日 OI 变化 %", "低"),
    ("ret1", "当日跌幅 %", "低"), ("top_ls", "大户多空比(账户)", "低"), ("global_ls", "散户多空比(账户)", "低"),
    ("taker", "主动买卖比", "高"), ("mvrv_z", "MVRV-Z", "低"), ("lower_wick", "下影线占比 %", "高"),
    ("vol_z", "成交量 z 分", "高"), ("low30_dist", "距 30 日低点 %", "低"),
    ("min_oi_4h", "日内 4h OI 最大跌幅 %", "低"), ("range", "当日振幅 %", "高"), ("down_wick", "下影线深度 %", "高"),
    ("n_prev14", "前 14 天清洗次数", "高"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--oi", type=float, default=-3.0, help="日内 4h OI 最大跌幅阈值 %")
    ap.add_argument("--ret", type=float, default=-2.0, help="当日跌幅阈值 %")
    ap.add_argument("--target", type=float, default=10.0, help="大反弹定义：14 天内最高收盘 ≥ +X%")
    ap.add_argument("--horizon", type=int, default=14)
    a = ap.parse_args()

    df = load_daily()
    print(f"样本 {df.index[0]} → {df.index[-1]}，{len(df)} 天")
    print("min_oi_4h 分位：", df["min_oi_4h"].quantile([.01, .02, .05, .10]).round(2).to_dict())
    print("ret1 分位：", df["ret1"].quantile([.01, .02, .05, .10]).round(2).to_dict())
    for oi_t, r_t in [(-2, -2), (-3, -2), (-3, -3), (-4, -3), (-5, -3), (-6, -4)]:
        print(f"  阈值 4h OI≤{oi_t}% & 跌≤{r_t}%：{len(find_events(df, oi_t, r_t))} 次事件")

    rho, top, j = validate_proxy(df)
    print(f"\n[代理验证] 2026-05-27 起 {len(j)} 天：真实多头强平美元 vs 各代理 Spearman ρ：",
          {k_: round(v_, 2) for k_, v_ in rho.items()})
    print(top.round(2).to_string())

    ev = find_events(df, a.oi, a.ret)
    rows = []
    for i in ev:
        f = forward(df, i, a.horizon)
        if f is None:
            continue
        r = {"date": df.index[i], "close": df["close"].iloc[i], **f}
        for key, _, _ in FEATURES:
            r[key] = df[key].iloc[i]
        rows.append(r)
    E = pd.DataFrame(rows).set_index("date")
    E["win"] = E["mfe"] >= a.target
    n = len(E)
    print(f"\n[事件] 日内 4h OI≤{a.oi}% & 当日跌≤{a.ret}%，冷却 5 天：{n} 次（含完整 {a.horizon} 天前瞻）")
    for t in (5, 10, 15, 20):
        print(f"  {a.horizon} 天内最高收盘 ≥ +{t}%：{(E['mfe'] >= t).mean() * 100:.0f}%（{(E['mfe'] >= t).sum()}/{n}）")
    print(f"  先再跌（MAE 中位）{E['mae'].median():.1f}%；MFE 中位 {E['mfe'].median():.1f}%；"
          f"7 日 {E['r7'].median():.1f}% / 14 日 {E['r14'].median():.1f}% / 30 日 {E['r30'].median():.1f}%（中位）")
    base_mfe = (df["close"].rolling(a.horizon).max().shift(-a.horizon) / df["close"] - 1) * 100
    print(f"  基准（任意一天）：{a.horizon} 天内最高收盘 ≥ +{a.target:.0f}% 的比例 {(base_mfe >= a.target).mean() * 100:.0f}%")
    print("\n[更长口径] 事件后 vs 任意一天（基准）：N 天内最高收盘 ≥ +X% 的比例")
    for h, col in ((30, "mfe30"), (60, "mfe60")):
        bm = (df["close"].rolling(h).max().shift(-h) / df["close"] - 1) * 100
        s = E[col].dropna()
        for t in (10, 15, 20, 30):
            print(f"  {h:>2} 天 ≥ +{t}%：事件后 {(s >= t).mean() * 100:>3.0f}%（{(s >= t).sum()}/{len(s)}） | 基准 {(bm >= t).mean() * 100:>3.0f}%")

    print(f"\n[配合条件] 以 {a.target:.0f}% 反弹为成功，每个指标按中位数切两半：")
    out = []
    for key, label, good in FEATURES:
        s = E[key].dropna()
        if len(s) < 10:
            continue
        med = s.median()
        lo, hi = E[E[key] <= med], E[E[key] > med]
        out.append((label, med, lo["win"].mean() * 100, len(lo), hi["win"].mean() * 100, len(hi),
                    lo["mfe"].median(), hi["mfe"].median(), good))
    out.sort(key=lambda x: -abs(x[2] - x[4]))
    print(f"{'指标':<22}{'中位':>9}{'≤中位 胜率':>12}{'n':>4}{'>中位 胜率':>12}{'n':>4}{'MFE中位 低/高':>16}")
    for label, med, wl, nl, wh, nh, ml, mh, _ in out:
        print(f"{label:<22}{med:>9.2f}{wl:>11.0f}%{nl:>4}{wh:>11.0f}%{nh:>4}{ml:>8.1f}/{mh:<6.1f}")

    # 次日确认
    for flag, name in ((True, "次日收涨"), (False, "次日续跌")):
        s = E[E["next_up"] == flag]
        print(f"  {name}：胜率 {s['win'].mean() * 100:.0f}%（n={len(s)}），MFE 中位 {s['mfe'].median():.1f}%")

    # 组合条件 / 牛熊分组（样本小，附 ±1.96·SE 作提醒）
    def show(name, mask):
        s = E[mask]
        if len(s) == 0:
            print(f"  {name:<34} n=0")
            return
        p = s["win"].mean()
        se = 1.96 * np.sqrt(p * (1 - p) / len(s)) * 100 if len(s) > 1 else 0
        print(f"  {name:<34} n={len(s):>2}  胜率 {p * 100:>3.0f}% ±{se:.0f}  MFE中位 {s['mfe'].median():>5.1f}%  "
              f"MAE中位 {s['mae'].median():>5.1f}%  30日中位 {s['r30'].median():>5.1f}%")
    print("\n[分组]")
    show("牛市（收盘 > 20 周均线）", E["ma140_dist"] > 0)
    show("熊市（收盘 ≤ 20 周均线）", E["ma140_dist"] <= 0)
    mo = E["min_oi_4h"].median()
    show(f"温和清洗（4h OI 跌 ≤ {abs(mo):.1f}%）", E["min_oi_4h"] > mo)
    show(f"剧烈清洗（4h OI 跌 > {abs(mo):.1f}%）", E["min_oi_4h"] <= mo)
    show("牛市 & 温和清洗", (E["ma140_dist"] > 0) & (E["min_oi_4h"] > mo))
    show("牛市 & 剧烈清洗", (E["ma140_dist"] > 0) & (E["min_oi_4h"] <= mo))
    show("熊市 & 温和清洗", (E["ma140_dist"] <= 0) & (E["min_oi_4h"] > mo))
    show("熊市 & 剧烈清洗", (E["ma140_dist"] <= 0) & (E["min_oi_4h"] <= mo))
    show("大振幅（≥ 8%）", E["range"] >= 8)
    show("深下影线（≥ 4%）", E["down_wick"] >= 4)
    show("投降式：RSI<35 & 恐贪<30", (E["rsi14"] < 35) & (E["fng"] < 30))
    show("投降式：前 7 日 ≤ −5% & 量 z>1", (E["ret7"] <= -5) & (E["vol_z"] > 1))
    show("大户多空比 > 1.68 & 次日收涨", (E["top_ls"] > 1.68) & E["next_up"])
    show("OI 7 日已缩 > 3.4%（杠杆提前退）", E["doi7"] <= E["doi7"].median())
    show("OI 7 日未缩（杠杆刚开始清）", E["doi7"] > E["doi7"].median())
    show("首次清洗（前 14 天 0 次）", E["n_prev14"] == 0)
    show("连环爆（前 14 天 ≥ 1 次）", E["n_prev14"] >= 1)
    show("连环爆尾声（前 14 天 ≥ 2 次）", E["n_prev14"] >= 2)
    show("连环爆 ≥1 & 前 7 日 ≤ −5%", (E["n_prev14"] >= 1) & (E["ret7"] <= -5))
    show("熊市 & 连环爆 ≥1 & 恐贪 < 30", (E["ma140_dist"] <= 0) & (E["n_prev14"] >= 1) & (E["fng"] < 30))
    print("\n[30 日口径分组] 成功 = 30 天内最高收盘 ≥ +15%")
    E["win"] = E["mfe30"] >= 15
    show("全部", E["mfe30"].notna())
    show("牛市（> 20 周均线）", E["ma140_dist"] > 0)
    show("熊市（≤ 20 周均线）", E["ma140_dist"] <= 0)
    show("首次清洗", E["n_prev14"] == 0)
    show("连环爆 ≥ 1", E["n_prev14"] >= 1)
    show("前 7 日 ≤ −2.9%（已跌一周）", E["ret7"] <= -2.9)
    show("散户多空比 ≤ 1.68", E["global_ls"] <= 1.68)
    show("MVRV-Z ≤ 0.69", E["mvrv_z"] <= 0.69)
    show("次日收涨", E["next_up"])
    show("已跌一周 & 散户多空 ≤1.68 & 次日收涨", (E["ret7"] <= -2.9) & (E["global_ls"] <= 1.68) & E["next_up"])

    E.round(2).to_csv(HERE / "liq_events.csv")
    print(f"\n事件明细已写 {HERE / 'liq_events.csv'}")
    print(E[["close", "ret1", "min_oi_4h", "range", "fng", "rsi14", "ma140_dist", "mfe", "mae", "r14", "r30", "win"]].round(1).to_string())


if __name__ == "__main__":
    main()
