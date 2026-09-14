import io
import json
import math
import os
import subprocess
import tempfile
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
    "Accept": "application/json,text/plain,*/*",
})


def get_json_with_retry(url, params=None, referer=None, attempts=5):
    """
    GitHub Actions 对部分国内站点偶尔会出现 RemoteDisconnected。
    这里采用：
      1. requests 重试
      2. curl 备用
      3. 逐次增加等待时间
    """
    headers = {
        "User-Agent": UA,
        "Accept": "application/json,text/plain,*/*",
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
                timeout=(15, 45),
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_error = e
            print(f"requests 第 {attempt}/{attempts} 次失败: {e}")

        # GitHub Actions 上 curl 有时比 Python requests 更稳定
        try:
            query_url = requests.Request(
                "GET", url, params=params
            ).prepare().url

            cmd = [
                "curl",
                "-L",
                "--retry", "2",
                "--retry-delay", "2",
                "--connect-timeout", "15",
                "--max-time", "60",
                "-sS",
                "-A", UA,
                "-H", f"Referer: {referer or url}",
                query_url,
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=75,
            )

            if result.returncode == 0 and result.stdout.strip():
                return json.loads(result.stdout)

            if result.stderr:
                print("curl 失败:", result.stderr.strip())
        except Exception as e:
            last_error = e
            print("curl 备用请求失败:", e)

        if attempt < attempts:
            wait = attempt * 3
            print(f"{wait} 秒后重试...")
            time.sleep(wait)

    raise RuntimeError(
        f"数据接口连续 {attempts} 次请求失败: {url}; "
        f"最后错误: {last_error}"
    )


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

    j = get_json_with_retry(
        url,
        params=params,
        referer="https://quote.eastmoney.com/",
    )

    ks = (j.get("data") or {}).get("klines") or []

    if not ks:
        raise RuntimeError("东方财富沪深300历史行情为空")

    rows = []

    for k in ks:
        x = k.split(",")
        if len(x) < 3:
            continue

        try:
            rows.append({
                "date": x[0],
                "index": float(x[2]),
            })
        except (TypeError, ValueError):
            pass

    df = pd.DataFrame(rows)

    if df.empty:
        raise RuntimeError("东方财富沪深300数据解析为空")

    df["date"] = pd.to_datetime(
        df["date"], errors="coerce"
    ).dt.strftime("%Y-%m-%d")

    df = df.dropna(subset=["date", "index"])

    if df.empty:
        raise RuntimeError("东方财富沪深300日期解析为空")

    return df[["date", "index"]].drop_duplicates("date")


def get_csindex_dividend():
    """
    沪深300历史股息率：中证指数公开估值 XLS。

    中证文件中的日期是 YYYYMMDD。
    必须按 %Y%m%d 解析，不能直接 pd.to_datetime()，
    否则会被误解析成 1970 年附近。
    """
    url = (
        "https://oss-ch.csindex.com.cn/static/html/csindex/"
        "public/uploads/file/autofile/indicator/000300indicator.xls"
    )

    last_error = None

    for attempt in range(1, 6):
        try:
            r = S.get(
                url,
                headers={
                    "User-Agent": UA,
                    "Referer": "https://www.csindex.com.cn/",
                },
                timeout=(20, 60),
            )
            r.raise_for_status()

            if len(r.content) < 1000:
                raise RuntimeError(
                    f"文件太小: {len(r.content)} bytes"
                )

            df = pd.read_excel(
                io.BytesIO(r.content),
                engine="xlrd",
            )

            if len(df.columns) < 10:
                raise RuntimeError("估值文件字段少于10列")

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

            df["date"] = pd.to_datetime(
                df["date"]
                .astype(str)
                .str.replace(r"\.0$", "", regex=True),
                format="%Y%m%d",
                errors="coerce",
            ).dt.strftime("%Y-%m-%d")

            df["dividend1"] = pd.to_numeric(
                df["dividend1"], errors="coerce"
            )

            df = df.dropna(
                subset=["date", "dividend1"]
            )

            df = df[
                (df["date"] >= "2008-01-01")
                & (df["date"] <= date.today().strftime("%Y-%m-%d"))
            ]

            if df.empty:
                raise RuntimeError(
                    "中证指数没有返回有效的2008年以后股息率"
                )

            # 文件单位：百分比。
            # 例如 2.79 -> 0.0279
            df["dividend_yield"] = df["dividend1"] / 100.0

            return (
                df[["date", "dividend_yield"]]
                .drop_duplicates("date")
                .sort_values("date")
            )

        except Exception as e:
            last_error = e
            print(
                f"中证指数第 {attempt}/5 次失败: {e}"
            )
            if attempt < 5:
                time.sleep(attempt * 3)

    raise RuntimeError(
        f"中证指数估值文件连续请求失败: {last_error}"
    )


def get_bond():
    """
    中国10年期国债到期收益率：
    东方财富公开宏观数据接口，无 Token。

    EMM00166466 = 10年期国债到期收益率。
    原始单位为百分比，例如 1.6899。
    输出转换为小数 0.016899。
    """
    url = "https://datacenter-web.eastmoney.com/api/data/get"

    rows = []
    page = 1
    page_size = 500

    while page <= 30:
        params = {
            "type": "RPTA_WEB_TREASURYYIELD",
            "sty": "ALL",
            "st": "SOLAR_DATE",
            "sr": "-1",
            "p": page,
            "ps": page_size,
        }

        j = get_json_with_retry(
            url,
            params=params,
            referer="https://data.eastmoney.com/cjsj/zmgzsyl.html",
        )

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

        if oldest < "2008-01-01":
            break

        if page >= pages:
            break

        page += 1

    df = pd.DataFrame(rows)

    if df.empty:
        raise RuntimeError(
            "东方财富没有返回中国10年期国债数据"
        )

    df = df.drop_duplicates("date")
    df = df[df["date"] >= "2008-01-01"]
    df = df.sort_values("date")

    if df.empty:
        raise RuntimeError(
            "10年期国债数据没有2008年以后的记录"
        )

    return df[["date", "bond_yield"]]


def main():
    print("1/3 获取沪深300指数...")
    idx = get_eastmoney_index()
    print(
        "指数:",
        len(idx),
        idx["date"].min(),
        idx["date"].max(),
    )

    print("2/3 获取沪深300股息率...")
    div = get_csindex_dividend()
    print(
        "股息率:",
        len(div),
        div["date"].min(),
        div["date"].max(),
    )

    print("3/3 获取中国10年期国债收益率...")
    bond = get_bond()
    print(
        "国债:",
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

    if not data:
        raise RuntimeError(
            "最终没有可用数据，停止生成 market.json"
        )

    payload = {
        "updated_at": pd.Timestamp.utcnow().isoformat(),
        "source": {
            "index": "Eastmoney",
            "dividend": "CSI Index",
            "bond": "Eastmoney",
        },
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

    latest = data[-1]

    print("")
    print("========================================")
    print("数据更新成功")
    print("文件:", OUT)
    print("记录数:", len(data))
    print("最新数据:", latest)
    print("========================================")


if __name__ == "__main__":
    main()
