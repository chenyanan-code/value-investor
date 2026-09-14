import json
import math
import os
import time
from datetime import date, timedelta

import pandas as pd
import requests

OUT = "data/market.json"
TOKEN = os.getenv("LIXINGER_TOKEN")
API_BASE = "https://open.lixinger.com/api"
START_DATE = "2008-01-01"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140 Safari/537.36"
)

S = requests.Session()
S.headers.update({
    "User-Agent": UA,
    "Content-Type": "application/json",
    "Accept": "application/json",
})


def check_token():
    if not TOKEN:
        raise RuntimeError(
            "缺少 LIXINGER_TOKEN。请在 GitHub Settings → "
            "Secrets and variables → Actions 中添加同名 Secret。"
        )


def daterange_chunks(start="2008-01-01"):
    """理杏仁单次时间范围不超过10年，因此分段请求。"""
    start_d = date.fromisoformat(start)
    end_d = date.today()

    while start_d <= end_d:
        # 留一点余量，确保不超过10年
        chunk_end = min(start_d.replace(year=start_d.year + 9) - timedelta(days=1), end_d)
        yield start_d.isoformat(), chunk_end.isoformat()
        start_d = chunk_end + timedelta(days=1)


def lixinger_post(path, payload, retries=3):
    url = f"{API_BASE}/{path}"

    body = dict(payload)
    body["token"] = TOKEN

    last_error = None

    for attempt in range(1, retries + 1):
        try:
            r = S.post(url, json=body, timeout=60)
            r.raise_for_status()
            j = r.json()

            if j.get("code") != 1:
                raise RuntimeError(
                    f"理杏仁 API 返回错误: code={j.get('code')}, "
                    f"message={j.get('message')}"
                )

            return j.get("data") or []

        except Exception as e:
            last_error = e
            if attempt < retries:
                wait = attempt * 3
                print(f"请求失败，第 {attempt} 次重试，等待 {wait}s: {e}")
                time.sleep(wait)

    raise RuntimeError(f"理杏仁 API 请求失败: {last_error}")


def get_index_and_dividend():
    """
    一次请求同时获取：
    - 沪深300收盘点位 cp
    - 沪深300总市值加权股息率 dyr.mcw

    理杏仁 API 返回的股息率本身为小数：
    0.0279 = 2.79%
    """
    frames = []

    for start, end in daterange_chunks(START_DATE):
        print(f"  理杏仁指数数据 {start} ~ {end}")

        rows = lixinger_post(
            "cn/index/fundamental",
            {
                "stockCodes": ["000300"],
                "startDate": start,
                "endDate": end,
                "metricsList": ["cp", "dyr.mcw"],
            },
        )

        if not rows:
            print("  本时间段没有返回数据")
            continue

        part = []
        for x in rows:
            d = x.get("date")
            cp = x.get("cp")
            dyr = x.get("dyr.mcw")

            if not d or cp is None or dyr is None:
                continue

            part.append({
                "date": str(d)[:10],
                "index": float(cp),
                "dividend_yield": float(dyr),
            })

        if part:
            frames.append(pd.DataFrame(part))

    if not frames:
        raise RuntimeError("理杏仁没有返回沪深300指数/股息率数据")

    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates("date").sort_values("date")

    # 基本合理性检查：避免异常数据悄悄进入网站
    if len(df) < 100:
        raise RuntimeError(f"沪深300指数/股息率有效数据仅 {len(df)} 条")

    if not df["index"].between(500, 10000).all():
        raise RuntimeError("沪深300指数数据存在明显异常值")

    if not df["dividend_yield"].between(0, 0.20).all():
        raise RuntimeError("沪深300股息率数据存在明显异常值")

    return df


def get_bond():
    """获取中国10年期国债到期收益率。"""
    frames = []

    for start, end in daterange_chunks(START_DATE):
        print(f"  理杏仁国债数据 {start} ~ {end}")

        rows = lixinger_post(
            "macro/national-debt",
            {
                "areaCode": "cn",
                "startDate": start,
                "endDate": end,
                "metricsList": ["tcm_y10"],
            },
        )

        if not rows:
            print("  本时间段没有返回数据")
            continue

        part = []
        for x in rows:
            d = x.get("date")
            y = x.get("tcm_y10")

            if not d or y is None:
                continue

            part.append({
                "date": str(d)[:10],
                "bond_yield": float(y),
            })

        if part:
            frames.append(pd.DataFrame(part))

    if not frames:
        raise RuntimeError("理杏仁没有返回中国10年期国债收益率数据")

    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates("date").sort_values("date")

    if len(df) < 100:
        raise RuntimeError(f"中国10年期国债有效数据仅 {len(df)} 条")

    if not df["bond_yield"].between(0, 0.20).all():
        raise RuntimeError("中国10年期国债收益率数据存在明显异常值")

    return df


def main():
    check_token()

    print("1/2 获取理杏仁沪深300指数 + 总市值加权股息率...")
    market = get_index_and_dividend()
    print(
        len(market),
        market["date"].min(),
        market["date"].max(),
    )

    print("2/2 获取理杏仁中国10年期国债收益率...")
    bond = get_bond()
    print(
        len(bond),
        bond["date"].min(),
        bond["date"].max(),
    )

    df = (
        market
        .merge(bond, on="date", how="inner")
        .sort_values("date")
    )

    if len(df) < 100:
        raise RuntimeError(
            f"三类数据有效重合日期仅 {len(df)} 条，拒绝生成不完整数据文件"
        )

    df["spread"] = df["dividend_yield"] - df["bond_yield"]

    df = (
        df.replace([math.inf, -math.inf], pd.NA)
        .dropna(subset=[
            "date",
            "index",
            "dividend_yield",
            "bond_yield",
            "spread",
        ])
    )

    data = []
    for _, r in df.iterrows():
        data.append({
            "date": r["date"],
            "index": round(float(r["index"]), 4),
            "dividend_yield": round(float(r["dividend_yield"]), 8),
            "bond_yield": round(float(r["bond_yield"]), 8),
            "spread": round(float(r["spread"]), 8),
        })

    payload = {
        "updated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "source": {
            "index": "Lixinger",
            "dividend": "Lixinger",
            "bond": "Lixinger",
        },
        "data": data,
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = OUT + ".tmp"

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            payload,
            f,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    os.replace(tmp, OUT)

    latest = data[-1]
    print("写入成功:", OUT)
    print("records =", len(data))
    print(
        "latest =",
        latest["date"],
        "index =", latest["index"],
        "dividend =", latest["dividend_yield"],
        "bond =", latest["bond_yield"],
        "spread =", latest["spread"],
    )


if __name__ == "__main__":
    main()
