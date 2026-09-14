import json
import math
import os
import re
import subprocess
import time
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
    "Accept": "text/html,application/xhtml+xml,application/json,text/plain,*/*",
})


def request_with_retry(url, params=None, referer=None, attempts=5):
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/json,text/plain,*/*",
    }
    if referer:
        headers["Referer"] = referer

    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            r = S.get(
                url,
                params=params,
                headers=headers,
                timeout=(15, 60),
            )
            r.raise_for_status()
            return r
        except Exception as e:
            last_error = e
            print(f"requests 第 {attempt}/{attempts} 次失败: {e}")

        try:
            query_url = requests.Request(
                "GET", url, params=params
            ).prepare().url

            cmd = [
                "curl", "-L",
                "--retry", "2",
                "--retry-delay", "2",
                "--connect-timeout", "15",
                "--max-time", "70",
                "-sS",
                "-A", UA,
                "-H", f"Referer: {referer or url}",
                query_url,
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=80,
            )

            if result.returncode == 0 and result.stdout.strip():
                class CurlResponse:
                    text = result.stdout
                    content = result.stdout.encode("utf-8")
                return CurlResponse()

            if result.stderr:
                print("curl 失败:", result.stderr.strip())

        except Exception as e:
            last_error = e
            print("curl 备用请求失败:", e)

        if attempt < attempts:
            time.sleep(attempt * 3)

    raise RuntimeError(
        f"请求失败: {url}; 最后错误: {last_error}"
    )


def get_eastmoney_index():
    """沪深300日线，东方财富公开接口，无 Token。"""
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

    r = request_with_retry(
        url,
        params=params,
        referer="https://quote.eastmoney.com/",
    )

    j = json.loads(r.text)
    ks = (j.get("data") or {}).get("klines") or []

    if not ks:
        raise RuntimeError("沪深300历史行情为空")

    rows = []

    for k in ks:
        x = k.split(",")
        if len(x) >= 3:
            try:
                rows.append({
                    "date": x[0],
                    "index": float(x[2]),
                })
            except ValueError:
                pass

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(
        df["date"], errors="coerce"
    ).dt.strftime("%Y-%m-%d")

    df = df.dropna(subset=["date", "index"])

    return df[["date", "index"]].drop_duplicates("date")


def get_legu_pe():
    """
    乐咕乐股沪深300历史PE。
    不需要 Token。
    """
    from io import StringIO

    # 注意：sz50 是上证50；沪深300对应 hs300。
    url = "https://legulegu.com/stockdata/hs300-ttm-lyr"

    r = request_with_retry(
        url,
        referer="https://legulegu.com/",
    )
    html = r.text

    if not html or "<html" not in html.lower():
        raise RuntimeError("乐咕乐股沪深300页面返回内容异常")

    rows = []

    # 先尝试页面中的 JSON/JS 数据。
    patterns = [
        r'"data"\s*:\s*(\[[\s\S]*?\])',
        r'"list"\s*:\s*(\[[\s\S]*?\])',
    ]

    for pattern in patterns:
        for m in re.finditer(pattern, html, re.I):
            candidate = m.group(1)
            try:
                obj = json.loads(candidate)
            except Exception:
                continue

            if not isinstance(obj, list):
                continue

            for x in obj:
                if not isinstance(x, dict):
                    continue

                d = (
                    x.get("date") or x.get("日期") or
                    x.get("Date") or x.get("trade_date")
                )
                pe = (
                    x.get("pe") or x.get("pe_ttm") or
                    x.get("滚动市盈率") or x.get("市盈率") or
                    x.get("PE") or x.get("PE_TTM")
                )

                if d is None or pe in (None, ""):
                    continue

                try:
                    value = float(str(pe).replace(",", "").strip())
                    if value > 0:
                        rows.append({
                            "date": str(d)[:10],
                            "pe": value,
                        })
                except (TypeError, ValueError):
                    pass

    # 如果 JSON 没有直接提供历史序列，再解析页面中的 HTML 表格。
    if not rows:
        try:
            # 使用 StringIO，避免 pandas 把 HTML 字符串误认为本地文件名。
            tables = pd.read_html(StringIO(html))
        except Exception as e:
            print("解析乐咕乐股HTML表格失败:", e)
            tables = []

        for table in tables:
            if table.empty:
                continue

            date_col = None
            pe_col = None

            for c in table.columns:
                name = str(c).strip()
                low = name.lower()

                if date_col is None and (
                    "日期" in name or
                    "date" in low or
                    "交易日" in name
                ):
                    date_col = c

                if pe_col is None and (
                    "滚动市盈率" in name or
                    "市盈率" in name or
                    low in {"pe", "pe_ttm", "ttm pe"} or
                    "ttm" in low
                ):
                    pe_col = c

            if date_col is None or pe_col is None:
                continue

            for _, x in table.iterrows():
                d = pd.to_datetime(
                    x[date_col],
                    errors="coerce",
                )

                try:
                    value = float(
                        str(x[pe_col]).replace(",", "").strip()
                    )
                except (TypeError, ValueError):
                    continue

                if pd.notna(d) and math.isfinite(value) and value > 0:
                    rows.append({
                        "date": d.strftime("%Y-%m-%d"),
                        "pe": value,
                    })

    df = pd.DataFrame(rows)

    if df.empty:
        raise RuntimeError(
            "无法从乐咕乐股页面取得沪深300历史PE"
        )

    df["date"] = pd.to_datetime(
        df["date"], errors="coerce"
    ).dt.strftime("%Y-%m-%d")

    df["pe"] = pd.to_numeric(
        df["pe"], errors="coerce"
    )

    df = df.dropna(subset=["date", "pe"])
    df = df[df["pe"] > 0]
    df = df[df["date"] >= "2008-01-01"]
    df = df.drop_duplicates("date").sort_values("date")

    if len(df) < 100:
        raise RuntimeError(
            f"乐咕乐股沪深300PE只有 {len(df)} 条，"
            "拒绝生成不完整数据"
        )

    return df[["date", "pe"]]


# 历史公开研究资料中的沪深300年度股息率（%）。
# 2008-2024 年用于构造无 Token 的长期估算序列。
# 这些是年度值，因此程序只用它们校准每年的 payout ratio，
# 再结合每日 PE 得到每日估算股息率。
HISTORICAL_ANNUAL_DIVIDEND_YIELD = {
    2008: 0.34,
    2009: 1.87,
    2010: 0.93,
    2011: 0.96,
    2012: 2.25,
    2013: 2.32,
    2014: 4.19,
    2015: 1.63,
    2016: 2.02,
    2017: 2.48,
    2018: 1.67,
    2019: 3.12,
    2020: 2.68,
    2021: 1.68,
    2022: 1.80,
    2023: 2.24,
    2024: 3.56,
}

# 对应年份沪深300平均PE（公开历史统计）。
# 用于估算当年 payout ratio = dividend yield / earnings yield。
HISTORICAL_ANNUAL_PE = {
    2008: 23.18,
    2009: 20.88,
    2010: 17.68,
    2011: 13.23,
    2012: 10.37,
    2013: 9.58,
    2014: 8.70,
    2015: 13.91,
    2016: 11.54,
    2017: 13.18,
    2018: 12.97,
    2019: 13.02,
    2020: 14.29,
    2021: 16.16,
    2022: 15.39,
    2023: 13.40,
    2024: 13.05,
    2025: 14.31,
}

# 2026 当前总市值加权股息率约 2.83%。
# 用当前公开值校准当前 payout ratio。
CURRENT_DIVIDEND_YIELD = 2.83


def get_dividend_yield(pe_df):
    """
    无 Token 的长期股息率估算。

    重要：
    这是“估算值”，不是理杏仁 API 的逐日原始值。
    计算思路：
        earnings_yield = 100 / PE
        dividend_yield = earnings_yield * payout_ratio

    2008-2024：
        使用公开年度沪深300股息率和年度平均PE反推年度 payout ratio。

    2025：
        使用 2025 年平均PE，并沿用 2024 年 payout ratio。

    2026：
        使用当前公开总市值加权股息率 2.83% 反推当前 payout ratio。
    """
    ratios = {}

    for year, dy in HISTORICAL_ANNUAL_DIVIDEND_YIELD.items():
        pe = HISTORICAL_ANNUAL_PE.get(year)
        if pe and pe > 0:
            ratios[year] = (dy / 100.0) / (1.0 / pe)

    if 2024 in ratios:
        ratios[2025] = ratios[2024]

    current_pe = HISTORICAL_ANNUAL_PE.get(2025, 14.31)
    ratios[2026] = (
        (CURRENT_DIVIDEND_YIELD / 100.0)
        / (1.0 / current_pe)
    )

    result = pe_df.copy()
    result["year"] = pd.to_datetime(
        result["date"]
    ).dt.year

    result["payout_ratio"] = result["year"].map(ratios)

    # 对于没有历史年度参数的情况，使用最近可用参数。
    result["payout_ratio"] = (
        result["payout_ratio"]
        .ffill()
        .bfill()
    )

    result["dividend_yield"] = (
        (1.0 / result["pe"])
        * result["payout_ratio"]
    )

    result["dividend_yield"] = (
        result["dividend_yield"]
        .clip(lower=0, upper=0.20)
    )

    return result[["date", "dividend_yield"]]


def get_bond():
    """中国10年期国债收益率，东方财富公开宏观接口，无 Token。"""
    url = "https://datacenter-web.eastmoney.com/api/data/get"

    rows = []
    page = 1

    while page <= 30:
        params = {
            "type": "RPTA_WEB_TREASURYYIELD",
            "sty": "ALL",
            "st": "SOLAR_DATE",
            "sr": "-1",
            "p": page,
            "ps": 500,
        }

        r = request_with_retry(
            url,
            params=params,
            referer="https://data.eastmoney.com/cjsj/zmgzsyl.html",
        )

        j = json.loads(r.text)
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

        oldest = min(
            (x["date"] for x in rows),
            default="9999-12-31",
        )

        if oldest < "2008-01-01" or page >= pages:
            break

        page += 1

    df = pd.DataFrame(rows)

    if df.empty:
        raise RuntimeError("10年期国债数据为空")

    df = df.drop_duplicates("date")
    df = df[df["date"] >= "2008-01-01"]
    df = df.sort_values("date")

    return df[["date", "bond_yield"]]


def main():
    print("1/3 获取沪深300指数...")
    idx = get_eastmoney_index()
    print(
        f"指数: {len(idx)} "
        f"{idx['date'].min()} {idx['date'].max()}"
    )

    print("2/3 获取沪深300历史PE并估算股息率...")
    pe = get_legu_pe()
    div = get_dividend_yield(pe)

    print(
        f"股息率: {len(div)} "
        f"{div['date'].min()} {div['date'].max()}"
    )

    print("3/3 获取中国10年期国债收益率...")
    bond = get_bond()
    print(
        f"国债: {len(bond)} "
        f"{bond['date'].min()} {bond['date'].max()}"
    )

    df = (
        idx
        .merge(div, on="date", how="inner")
        .merge(bond, on="date", how="inner")
        .sort_values("date")
    )

    if len(df) < 1000:
        raise RuntimeError(
            f"三类数据有效重合日期仅 {len(df)} 条，"
            "拒绝生成不完整数据文件"
        )

    df["spread"] = (
        df["dividend_yield"] - df["bond_yield"]
    )

    df = (
        df.replace([math.inf, -math.inf], pd.NA)
        .dropna(
            subset=[
                "date",
                "index",
                "dividend_yield",
                "bond_yield",
                "spread",
            ]
        )
    )

    data = []

    for _, r in df.iterrows():
        data.append({
            "date": r["date"],
            "index": round(float(r["index"]), 4),
            "dividend_yield": round(
                float(r["dividend_yield"]), 8
            ),
            "bond_yield": round(
                float(r["bond_yield"]), 8
            ),
            "spread": round(
                float(r["spread"]), 8
            ),
        })

    payload = {
        "updated_at": pd.Timestamp.utcnow().isoformat(),
        "source": {
            "index": "Eastmoney",
            "dividend": "Legu PE + historical payout-ratio estimate",
            "bond": "Eastmoney",
        },
        "data_note": (
            "Dividend yield is a no-token historical estimate. "
            "It is not a direct export of the Lixinger API."
        ),
        "data": data,
    }

    os.makedirs(
        os.path.dirname(OUT),
        exist_ok=True,
    )

    tmp = OUT + ".tmp"

    with open(
        tmp,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            payload,
            f,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    os.replace(tmp, OUT)

    print("")
    print("========================================")
    print("数据更新成功")
    print("文件:", OUT)
    print("记录数:", len(data))
    print("最新数据:", data[-1])
    print("========================================")


if __name__ == "__main__":
    main()
