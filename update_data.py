import json
import math
import os
import subprocess
import time
from datetime import date
from io import StringIO

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

            if result.stderr.strip():
                print(f"curl 失败: {result.stderr.strip()}")

        except Exception as e:
            print(f"curl 异常: {e}")

        time.sleep(attempt * 3)

    raise RuntimeError(
        f"请求失败: {url}; 最后错误: {last_error}"
    )


def get_eastmoney_index():
    """沪深300日线，东方财富公开接口，无 Token。

    GitHub Actions 有时会被 push2his.eastmoney.com 直接断开连接，
    因此准备多个东方财富历史行情节点作为备用。
    """
    urls = [
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        "https://61.push2his.eastmoney.com/api/qt/stock/kline/get",
        "https://33.push2his.eastmoney.com/api/qt/stock/kline/get",
    ]

    params = {
        "secid": "1.000300",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",
        "fqt": "0",
        "beg": "20080101",
        "end": date.today().strftime("%Y%m%d"),
        "lmt": "6000",
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
        "rtntype": "6",
        "_": str(int(time.time() * 1000)),
    }

    last_error = None

    for url in urls:
        print(f"尝试东方财富历史行情节点: {url}")
        try:
            r = request_with_retry(
                url,
                params=params,
                referer="https://quote.eastmoney.com/",
                attempts=3,
            )

            j = json.loads(r.text)
            ks = (j.get("data") or {}).get("klines") or []

            if not ks:
                last_error = RuntimeError("返回的 klines 为空")
                print("该节点没有返回历史行情，尝试下一个节点...")
                continue

            rows = []
            for k in ks:
                x = k.split(",")
                if len(x) >= 3:
                    try:
                        rows.append({
                            "date": x[0],
                            "index": float(x[2]),
                        })
                    except (ValueError, TypeError):
                        pass

            df = pd.DataFrame(rows)
            if df.empty:
                last_error = RuntimeError("无法解析沪深300历史行情")
                continue

            df["date"] = pd.to_datetime(
                df["date"], errors="coerce"
            ).dt.strftime("%Y-%m-%d")
            df = df.dropna(subset=["date", "index"])
            df = df.drop_duplicates("date").sort_values("date")

            if len(df) < 1000:
                last_error = RuntimeError(
                    f"沪深300历史行情只有 {len(df)} 条"
                )
                print(f"该节点数据不足：{len(df)} 条，尝试下一个节点...")
                continue

            print(
                f"东方财富历史行情节点成功：{len(df)} 条"
            )
            print(
                f"指数: {len(df)} "
                f"{df['date'].iloc[0]} {df['date'].iloc[-1]}"
            )
            return df[["date", "index"]]

        except Exception as e:
            last_error = e
            print(f"该节点失败: {e}")
            print("尝试下一个东方财富历史行情节点...")

    raise RuntimeError(
        "东方财富所有历史行情节点均无法访问。"
        f"最后错误: {last_error}"
    )


def get_legu_pe():
    """
    使用 AKShare 的 stock_index_pe_lg() 获取乐咕乐股沪深300历史 PE。

    这是无 Token 方案。
    AKShare 官方接口名称为 stock_index_pe_lg，
    symbol 使用中文“沪深300”。

    返回：
        date: YYYY-MM-DD
        pe: 滚动市盈率（TTM）
    """
    try:
        import akshare as ak
    except ImportError as e:
        raise RuntimeError(
            "未安装 AKShare。请确认 requirements.txt 已加入 akshare。"
        ) from e

    last_error = None

    for attempt in range(1, 4):
        try:
            print(
                f"调用 AKShare stock_index_pe_lg('沪深300') "
                f"第 {attempt}/3 次..."
            )

            df = ak.stock_index_pe_lg(symbol="沪深300")

            if df is None or df.empty:
                raise RuntimeError("AKShare 返回空数据")

            print(f"AKShare 返回 {len(df)} 条 PE 数据")
            print("PE 字段:", list(df.columns))

            # AKShare 当前接口通常返回：
            # 日期、指数、等权静态市盈率、静态市盈率、
            # 静态市盈率中位数、等权滚动市盈率、滚动市盈率、
            # 滚动市盈率中位数
            date_col = None
            for col in df.columns:
                name = str(col).strip()
                if name == "日期" or name.lower() == "date":
                    date_col = col
                    break

            if date_col is None:
                # 兼容未来版本可能返回英文日期字段
                for col in df.columns:
                    if "日期" in str(col) or "date" in str(col).lower():
                        date_col = col
                        break

            pe_col = None

            # 优先使用滚动市盈率（TTM），而不是静态市盈率。
            preferred_names = [
                "滚动市盈率",
                "TTM市盈率",
                "PE-TTM",
                "pe_ttm",
            ]

            for wanted in preferred_names:
                for col in df.columns:
                    if str(col).strip() == wanted:
                        pe_col = col
                        break
                if pe_col is not None:
                    break

            # 兼容列名变化
            if pe_col is None:
                for col in df.columns:
                    name = str(col).strip().lower()
                    if (
                        "滚动市盈率" in str(col)
                        or "ttm" in name
                    ):
                        pe_col = col
                        break

            if date_col is None:
                raise RuntimeError(
                    f"AKShare 返回数据中找不到日期字段: {list(df.columns)}"
                )

            if pe_col is None:
                raise RuntimeError(
                    f"AKShare 返回数据中找不到滚动市盈率字段: {list(df.columns)}"
                )

            out = pd.DataFrame({
                "date": df[date_col],
                "pe": df[pe_col],
            })

            # 注意：
            # 当前 AKShare 某些版本返回的“日期”已经是 datetime/date，
            # 不要把它当毫秒时间戳再次转换。
            out["date"] = pd.to_datetime(
                out["date"], errors="coerce"
            )

            out["pe"] = pd.to_numeric(
                out["pe"], errors="coerce"
            )

            out = out.dropna(subset=["date", "pe"])
            out = out[out["pe"] > 0]
            out = out[out["date"] >= pd.Timestamp("2008-01-01")]
            out = out.drop_duplicates("date")
            out = out.sort_values("date")

            out["date"] = out["date"].dt.strftime("%Y-%m-%d")

            if len(out) < 100:
                raise RuntimeError(
                    f"AKShare 沪深300历史PE只有 {len(out)} 条，"
                    "拒绝生成不完整数据"
                )

            print(
                f"沪深300历史PE成功：{len(out)} 条 "
                f"{out['date'].iloc[0]} {out['date'].iloc[-1]}"
            )

            return out[["date", "pe"]]

        except Exception as e:
            last_error = e
            print(f"AKShare 第 {attempt}/3 次失败: {e}")
            if attempt < 3:
                time.sleep(attempt * 5)

    raise RuntimeError(
        "无法通过 AKShare 获取沪深300历史PE。"
        f"最后错误: {last_error}"
    )


# 历史公开研究资料中的沪深300年度股息率（%）。
# 用于构造无 Token 的长期估算序列。
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

# 当前公开的沪深300总市值加权股息率参考值。
CURRENT_DIVIDEND_YIELD = 2.83


def get_dividend_yield(pe_df):
    """
    根据历史年度股息率 + PE 反推 payout ratio，
    再结合每日沪深300 TTM PE 估算每日股息率。

    重要：
    这里的 dividend_yield 是“无 Token 历史估算值”，
    不是理杏仁 API 的逐日原始股息率。
    """
    ratios = {}

    for year, dy in HISTORICAL_ANNUAL_DIVIDEND_YIELD.items():
        pe = HISTORICAL_ANNUAL_PE.get(year)
        if pe and pe > 0:
            ratios[year] = (dy / 100.0) / (1.0 / pe)

    # 2025 沿用 2024 payout ratio
    if 2024 in ratios:
        ratios[2025] = ratios[2024]

    # 2026 用当前参考股息率反推当前 payout ratio
    current_pe = HISTORICAL_ANNUAL_PE.get(2025, 14.31)
    ratios[2026] = (
        (CURRENT_DIVIDEND_YIELD / 100.0)
        / (1.0 / current_pe)
    )

    result = pe_df.copy()
    result["year"] = pd.to_datetime(result["date"]).dt.year
    result["payout_ratio"] = result["year"].map(ratios)

    # 对没有对应年度参数的数据，使用最近可用参数。
    result["payout_ratio"] = (
        result["payout_ratio"]
        .ffill()
        .bfill()
    )

    result["dividend_yield"] = (
        (1.0 / result["pe"])
        * result["payout_ratio"]
    )

    # 防止异常值污染网站数据
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
                        # 接口字段是百分数，例如 1.6899，
                        # 网站 JSON 统一保存为小数 0.016899。
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

    print(
        f"10年期国债收益率成功：{len(df)} 条 "
        f"{df['date'].iloc[0]} {df['date'].iloc[-1]}"
    )

    return df[["date", "bond_yield"]]


def main():
    print("1/3 获取沪深300指数...")
    idx = get_eastmoney_index()

    print("")
    print("2/3 获取沪深300历史PE并估算股息率...")
    pe = get_legu_pe()
    dy = get_dividend_yield(pe)

    print(
        f"股息率估算成功：{len(dy)} 条 "
        f"{dy['date'].iloc[0]} {dy['date'].iloc[-1]}"
    )

    print("")
    print("3/3 获取中国10年期国债收益率...")
    bond = get_bond()

    df = idx.merge(dy, on="date", how="inner")
    df = df.merge(bond, on="date", how="inner")

    df = df.sort_values("date")

    # 股债利差 = 沪深300估算股息率 - 10年期国债收益率
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

    if len(df) < 1000:
        raise RuntimeError(
            f"最终合并数据只有 {len(df)} 条，"
            "不足以生成长期历史数据，停止写入 market.json"
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
            "pe": "AKShare stock_index_pe_lg / Legu",
            "dividend": "Historical payout-ratio estimate based on CSI 300 PE",
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
    print("最早:", data[0])
    print("最新:", data[-1])
    print("========================================")


if __name__ == "__main__":
    main()
