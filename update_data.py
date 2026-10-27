#!/usr/bin/env python3
"""BTC 周期定位页 · 每日数据更新

拉 Bybit 公开行情 + alternative.me 恐贪，把 template.html 渲染成 btc-cycle-position.html。
判断类文字来自 data.json（手动字段），脚本只负责数字。
结尾打印摘要和 ALERT 行，供每日任务决定要不要改 data.json 的判断。

用法：python3 update_data.py
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from string import Template

import requests

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "template.html"
DATA = HERE / "data.json"
HISTORY = HERE / "history.json"
OUT = HERE / "btc-cycle-position.html"          # Artifact 用（无 doctype，由平台包壳）
OUT_PAGES = HERE / "docs" / "index.html"        # GitHub Pages 用（完整文档）

PAGES_HEAD = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
              '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
              '<meta name="apple-mobile-web-app-capable" content="yes">'
              '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">'
              '<title>BTC 周期定位</title>'
              '<style>:root{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}</style>'
              '</head><body>')
PAGES_TAIL = '</body></html>'

KL = timezone(timedelta(hours=8))
BYBIT = "https://api.bybit.com/v5/market"
HALVING_NEXT = datetime(2028, 4, 1, tzinfo=KL)


def get(url, params):
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    j = r.json()
    if "retCode" in j and j["retCode"] != 0:
        raise RuntimeError(f"Bybit 错误: {j.get('retMsg')}")
    return j


def bybit(path, **params):
    params.setdefault("category", "linear")
    params.setdefault("symbol", "BTCUSDT")
    return get(f"{BYBIT}/{path}", params)["result"]["list"]


def k(v):
    """80412 -> '80.4k'"""
    return f"{v / 1000:.1f}k"


def pct(a, b):
    return (a / b - 1) * 100


def fetch():
    t = bybit("tickers")[0]
    price = float(t["lastPrice"])
    funding = float(t["fundingRate"]) * 100  # %

    # 日线 200 根：均线、30 日低、60 日高
    daily = bybit("kline", interval="D", limit=200)  # 新 → 旧
    closes = [float(c[4]) for c in daily]
    highs = [float(c[2]) for c in daily]
    lows = [float(c[3]) for c in daily]
    sma30 = sum(closes[:30]) / 30
    ma200 = sum(closes) / len(closes)
    res60 = max(highs[1:61])  # 不含今天
    sup30 = min(lows[1:31])

    # 周线 200 根：ATH 与本轮低点
    weekly = bybit("kline", interval="W", limit=200)
    wk = [(datetime.fromtimestamp(int(c[0]) / 1000, KL), float(c[2]), float(c[3]), float(c[4])) for c in weekly]
    ath_dt, ath = max(((d, h) for d, h, _, _ in wk), key=lambda x: x[1])
    after = [(d, l) for d, _, l, _ in wk if d > ath_dt]
    low_dt, low = min(after, key=lambda x: x[1]) if after else (ath_dt, price)
    # 上一根已收周线（index 1）是否站上 60 日高
    last_closed_week_close = wk[1][3] if len(wk) > 1 else price
    weekly_break = last_closed_week_close > res60

    # OI 60 日
    oi = bybit("open-interest", intervalTime="1d", limit=60)
    oi_now, oi_then = float(oi[0]["openInterest"]), float(oi[-1]["openInterest"])
    oi60 = pct(oi_now, oi_then)

    # 多空比
    ls = bybit("account-ratio", period="4h", limit=1)[0]
    buy = float(ls["buyRatio"])
    ls_ratio = buy / float(ls["sellRatio"])

    # 恐贪
    fng = get("https://api.alternative.me/fng/", {"limit": 1})["data"][0]
    fng_now = int(fng["value"])

    return dict(price=price, funding=funding, sma30=sma30, ma200=ma200, res60=res60, sup30=sup30,
                ath=ath, ath_dt=ath_dt, low=low, low_dt=low_dt, weekly_break=weekly_break,
                oi60=oi60, ls_ratio=ls_ratio, ls_long=buy * 100, fng=fng_now)


def load_history():
    try:
        return json.loads(HISTORY.read_text())
    except FileNotFoundError:
        return {}


def row(state, label, title, note):
    return (f'    <li class="row"><span class="chip {state}">{label}</span>'
            f'<div><b>{title}</b><small>{note}</small></div></li>')


def build(live, data, history):
    now = datetime.now(KL)
    today = now.strftime("%Y-%m-%d")
    months = (now.year - live["ath_dt"].year) * 12 + now.month - live["ath_dt"].month
    months_to_halving = (HALVING_NEXT.year - now.year) * 12 + HALVING_NEXT.month - now.month

    # 恐贪 30 天前
    hist = history.get("fng", {})
    hist[today] = live["fng"]
    past = [(d, v) for d, v in hist.items() if d <= (now - timedelta(days=30)).strftime("%Y-%m-%d")]
    fng_prev = max(past)[1] if past else None
    fng_note = f"一个月前 {fng_prev}" if fng_prev else "6 月极度恐惧时曾 8–12"

    v = dict(
        asof=now.strftime("%Y-%m-%d %H:%M"),
        price=f"{live['price']:,.0f}",
        months=months, months_to_halving=months_to_halving,
        chg_ath=f"{pct(live['price'], live['ath']):+.0f}%".replace("-", "−"),
        ath_k=k(live["ath"]), ath_ym=live["ath_dt"].strftime("%Y-%m"),
        chg_low=f"{pct(live['price'], live['low']):+.0f}%",
        low_k=k(live["low"]), low_ym=live["low_dt"].strftime("%Y-%m"),
        funding=f"{live['funding']:+.3f}%",
        ls_ratio=f"{live['ls_ratio']:.2f}",
        res_k=k(live["res60"]), sup_k=k(live["sup30"]),
        sma30_k=k(live["sma30"]), ma200_k=k(live["ma200"]),
        oi60=f"{live['oi60']:+.0f}%",
        fng=str(live["fng"]), fng_note=fng_note,
        drawdown=f"−{abs(pct(live['price'], live['ath'])):.0f}%",
    )

    # 自动语义标签
    f = live["funding"]
    v["funding_cls"], v["funding_note"] = (
        ("t-bad", "过热，杠杆多头拥挤") if f > 0.05 else
        ("t-good", "负费率，空头拥挤") if f < -0.01 else
        ("t-good", "中性，杠杆已清"))
    r = live["ls_ratio"]
    v["ls_cls"], v["ls_note"] = (
        ("t-bad", f"{live['ls_long']:.0f}% 散户做多，拥挤") if r > 1.5 else
        ("t-good", f"{live['ls_long']:.0f}% 做多，空头拥挤") if r < 0.7 else
        ("", f"{live['ls_long']:.0f}% 做多，均衡"))
    v["oi_cls"], v["oi_note"] = (("t-warn", "价格横盘，仓位在堆") if live["oi60"] > 10 else
                                 ("t-good", "杠杆在退出") if live["oi60"] < -10 else ("", "持平"))
    dd = abs(pct(live["price"], live["ath"]))
    v["dd_note"] = "异常浅" if dd < 65 else "接近历史"

    # 手动字段（文本内再做一次 $ 替换）
    sub = lambda s: Template(s).safe_substitute(v)
    v["title"] = f"{data['regime']}第 {months} 个月，{data['phase_label']}"
    v["verdict_big"] = sub(data["verdict_big"])
    v["verdict_sub"] = sub(data["verdict_sub"])
    v["clock_note"] = sub(data["clock_note"])
    for i in range(1, 7):
        v[f"ph{i}"] = "done" if i < data["phase_index"] else "now" if i == data["phase_index"] else ""

    # 手动数据格先放进 v，条件/清单文本里才能引用 $fed_rate 等
    for key in ("etf", "fed", "whale"):
        for kk, vv in data[key].items():
            v[f"{key}_{kk}"] = sub(vv)

    conds = data["conditions"]
    v["cond_rows"] = "\n".join(row(c["state"], c["label"], sub(c["title"]), sub(c["note"])) for c in conds)
    v["cond_score"] = sum(1 for c in conds if c["state"] == "ok")

    rows, score = [], 0
    for c in data["checklist"]:
        state, label = c["state"], c["label"]
        if state == "auto_weekly_break":
            state, label = ("ok", "✓") if live["weekly_break"] else ("no", "✗")
        score += state == "ok"
        rows.append(row(state, label, sub(c["title"]), sub(c["note"])))
    v["check_rows"], v["check_score"] = "\n".join(rows), score

    # 价位梯：自动 + 手动，按价格降序，现价插入高亮
    levels = [
        (live["res60"], None, "60 日高点", f"周线站上 = 趋势反转"),
        (live["sma30"], None, "30 日均线", ""),
        (live["sup30"], None, "生死线", "30 日低点，破了别抄底"),
        (live["ma200"], None, "200 日均线", ""),
        (live["price"], "NOW", "现价", "箱体中轴，赔率最差"),
    ]
    levels += [(m["k"], m.get("label"), m["tag"], m.get("sub", "")) for m in data["manual_levels"]]
    lad = []
    for p, label, tag, s in sorted(levels, key=lambda x: -x[0]):
        cls = ' class="now"' if label == "NOW" else ""
        shown = k(p) if label in (None, "NOW") else label
        small = f" <small>{sub(s)}</small>" if s else ""
        lad.append(f'    <li{cls}><span class="lv num">{shown}</span><span class="tag">{tag}{small}</span></li>')
    v["ladder_rows"] = "\n".join(lad)
    # 给页面实时脚本用：手动价位（sub 已替换）与高点时间戳
    v["manual_levels_json"] = json.dumps(
        [{"k": m["k"], "label": m.get("label"), "tag": m["tag"], "sub": sub(m.get("sub", ""))} for m in data["manual_levels"]],
        ensure_ascii=False)
    v["ath_ts"] = int(live["ath_dt"].timestamp() * 1000)

    v["scenario_rows"] = "\n".join(
        f'    <div><div class="head"><span>{s["name"]}</span><span class="num">{s["pct"]}%</span></div>'
        f'<div class="meter {s["cls"]}"><i style="width:{s["pct"]}%"></i></div><p>{sub(s["text"])}</p></div>'
        for s in data["scenarios"])
    v["action_rows"] = "\n".join(
        f'    <li><span class="n">{i}</span><span>{sub(a)}</span></li>' for i, a in enumerate(data["actions"], 1))
    v["event_date"], v["event_text"] = data["event"]["date"], sub(data["event"]["text"])
    v["footer_notes"] = data["footer_notes"]

    history["fng"] = hist
    history.setdefault("price", {})[today] = live["price"]
    return v


def alerts(live):
    out = []
    if live["price"] < live["sup30"]:
        out.append(f"ALERT 跌破生死线 {k(live['sup30'])}，现价 {k(live['price'])}")
    if live["weekly_break"]:
        out.append(f"ALERT 上周线收盘站上 60 日高点 {k(live['res60'])}")
    if live["funding"] > 0.05:
        out.append(f"ALERT 费率过热 {live['funding']:+.3f}%")
    if live["price"] < live["low"] * 1.02:
        out.append(f"ALERT 逼近本轮低点 {k(live['low'])}")
    return out


def main():
    data = json.loads(DATA.read_text())
    history = load_history()
    live = fetch()
    v = build(live, data, history)
    body = Template(TEMPLATE.read_text()).substitute(v)
    OUT.write_text(body)
    # Pages 版：去掉 body 里的 <title>、claude 专用 meta 和「打开实时版」入口，套完整文档壳
    pages_body = "\n".join(l for l in body.splitlines()
                           if not l.startswith("<title>") and "claude:viewport-fit-sides" not in l
                           and 'class="livelink"' not in l)
    OUT_PAGES.parent.mkdir(exist_ok=True)
    OUT_PAGES.write_text(PAGES_HEAD + "\n" + pages_body + "\n" + PAGES_TAIL)
    HISTORY.write_text(json.dumps(history, ensure_ascii=False, indent=1))

    print(f"已渲染 {OUT.name} + docs/index.html · {v['asof']}")
    print(f"价格 {v['price']} | 距高点 {v['chg_ath']} | 距低点 {v['chg_low']} | 第 {v['months']} 个月")
    print(f"费率 {v['funding']} | 多空比 {v['ls_ratio']} | OI60 {v['oi60']} | 恐贪 {v['fng']}")
    print(f"阻力 {v['res_k']} | SMA30 {v['sma30_k']} | 支撑 {v['sup_k']} | MA200 {v['ma200_k']}")
    a = alerts(live)
    print("\n".join(a) if a else "无 ALERT")
    return 0


if __name__ == "__main__":
    sys.exit(main())
