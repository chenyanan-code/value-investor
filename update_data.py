import io
import json
import math
import os
from datetime import date

import pandas as pd
import requests


OUT = "data/market.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0 Safari/537.36"
)

S = requests.Session()
S.headers.update({
    "User-Agent": UA,
    "Accept": "application/json,text/plain,*/*",
})


def get_eastmoney_index():
    """沪深300日线：东方财富公开接口，无 Token。"""
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": "1.000300",
        "fields1": "f1,f2,f3",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
        "klt": "101",
        "fqt": "0",
        "beg": "20080101",
        "end": date.today().strftime("%Y%m%d"),
        "lmt": "6000",
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
    }

    r = S.get(
        url,
        params=params,
        headers={"Referer": "https://quote.eastmoney.com/"},
        timeout=30,
    )
    r.raise_for_status()

    j = r.json()
    ks = (j.get("data") or {}).get("klines") or []

    if not ks:
        raise RuntimeError("东方财富沪深300历史行情为空")

    rows = []
    for k in ks:
        x = k.split(",")
        if len(x) < 3:
            continue
        rows.append({
            "date": x[0],
            "index": float(x[2]),
        })

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df = df.dropna(subset=["date", "index"])

    if df.empty:
        raise RuntimeError("东方财富沪深300数据解析为空")

    return df[["date", "index"]]


def get_csindex_dividend():
    """
    沪深300历史股息率：中证指数公开估值文件。

    注意：
    这个 XLS 的日期是 YYYYMMDD 数字。
    之前代码直接 pd.to_datetime() 会把它错误解析成 1970 年，
    导致 GitHub Actions 只得到类似 1970-01-01 的日期。
    """
    url = (
        "https://oss-ch.csindex.com.cn/static/html/csindex/"
        "public/uploads/file/autofile/indicator/000300indicator.xls"
    )

    r = S.get(url, timeout=60)
    r.raise_for_status()

    content_type = (r.headers.get("Content-Type") or "").lower()
    if len(r.content) < 1000:
        raise RuntimeError(
            f"中证指数估值文件异常，文件大小只有 {len(r.content)} bytes"
        )

    df = pd.read_excel(
        io.BytesIO(r.content),
        engine="xlrd",
    )

    if len(df.columns) < 10:
        raise RuntimeError("中证指数估值文件字段异常")

    df = df.iloc[:, :10].copy()
    df.columns = [
        "date",
        "code",
        "name_cn",
        "name_short",
        "name_en",
        "short_en",
        "pe1",
        "pe2",
        "dividend1",
        "dividend2",
    ]

    # 关键修复：中证文件日期格式为 YYYYMMDD
    df["date"] = pd.to_datetime(
        df["date"].astype(str).str.replace(r"\.0$", "", regex=True),
        format="%Y%m%d",
        errors="coerce",
    ).dt.strftime("%Y-%m-%d")

    df["dividend1"] = pd.to_numeric(
        df["dividend1"], errors="coerce"
    )

    df = df.dropna(subset=["date", "dividend1"])
    df = df[df["date"] >= "2008-01-01"]

    if df.empty:
        raise RuntimeError("中证指数没有返回有效的沪深300股息率历史数据")

    # dividend1 的单位是百分比，例如 2.79 -> 0.0279
    df["dividend_yield"] = df["dividend1"] / 100.0

    return df[["date", "dividend_yield"]].drop_duplicates("date")


def get_bond():
    """
    中国10年期国债到期收益率：东方财富公开宏观数据接口，无 Token。

    EMM00166466 = 10年期国债到期收益率。
    例如 2026-09-11 返回 1.6899，代表 1.6899%。
    """
    url = "https://datacenter-web.eastmoney.com/api/data/get"

    rows = []
    page = 1
    page_size = 500

    while True:
        params = {
            "type": "RPTA_WEB_TREASURYYIELD",
            "sty": "ALL",
            "st": "SOLAR_DATE",
            "sr": "-1",
            "p": page,
            "ps": page_size,
        }

        r = S.get(
            url,
            params=params,
            headers={"Referer": "https://data.eastmoney.com/cjsj/zmgzsyl.html"},
            timeout=45,
        )
        r.raise_for_status()

        j = r.json()
        result = j.get("result") or {}
        data = result.get("data") or []

        if not data:
            break

        for x in data:
            d = x.get("SOLAR_DATE")
            y = x.get("EMM00166466")

            if d and y not in (None, ""):
                try:
                    rows.append({
                        "date": str(d)[:10],
                        "bond_yield": float(y) / 100.0,
                    })
                except (TypeError, ValueError):
                    pass

        pages = int(result.get("pages") or page)

        if page >= pages:
            break

        page += 1

        # 数据按日期倒序。
        # 如果已经早于 2008 年，则没有必要继续翻页。
        oldest = min(
            (r["date"] for r in rows),
            default="9999-12-31",
        )
        if oldest < "2008-01-01":
            break

        if page > 30:
            # 防止接口异常时无限循环
            break

    df = pd.DataFrame(rows)

    if df.empty:
        raise RuntimeError("东方财富没有返回中国10年期国债数据")

    df = df.drop_duplicates("date")
    df = df[df["date"] >= "2008-01-01"]
    df = df.sort_values("date")

    if df.empty:
        raise RuntimeError("东方财富10年期国债数据没有2008年以后的记录")

    return df[["date", "bond_yield"]]


def main():
    print("1/3 获取东方财富沪深300...")
    idx = get_eastmoney_index()
    print(
        len(idx),
        idx["date"].min(),
        idx["date"].max(),
    )

    print("2/3 获取中证指数沪深300股息率...")
    div = get_csindex_dividend()
    print(
        len(div),
        div["date"].min(),
        div["date"].max(),
    )

    print("3/3 获取东方财富10年期国债收益率...")
    bond = get_bond()
    print(
        len(bond),
        bond["date"].min(),
        bond["date"].max(),
    )

    df = (
        idx
        .merge(div, on="date", how="inner")
        .merge(bond, on="date", how="inner")
        .sort_values("date")
    )

    if len(df) < 100:
        raise RuntimeError(
            f"三类数据有效重合日期仅 {len(df)} 条，"
            "拒绝生成不完整数据文件"
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
        "updated_at": pd.Timestamp.utcnow().isoformat(),
        "source": {
            "index": "Eastmoney",
            "dividend": "CSI Index",
            "bond": "Eastmoney",
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

    print("")
    print("========================================")
    print("写入成功:", OUT)
    print("records =", len(data))
    print("latest  =", latest)
    print("========================================")


if __name__ == "__main__":
    main()
