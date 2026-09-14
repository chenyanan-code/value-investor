# 股债利差 V4

本项目用于 GitHub Pages 展示沪深300股债利差。

## 数据源

- 沪深300历史行情：东方财富
- 沪深300股息率：中证指数公开指数估值文件
- 中国10年期国债收益率：中国货币网

股债利差 = 沪深300股息率 - 中国10年期国债收益率。

本版本不需要理杏仁账号，也不需要 LIXINGER_TOKEN。

## GitHub 部署

1. 将整个项目上传到 GitHub。
2. Actions → Update market data → Run workflow。
3. 等待绿色 Success。
4. Settings → Pages → Deploy from a branch → 选择 main / root。
5. 打开 GitHub Pages 地址。

## 目录

```text
index.html
update_data.py
requirements.txt
README.md
data/market.json
.github/workflows/update.yml
```

## 重要

`update_data.py` 只有在三类数据都成功获取且有效重合日期不少于100条时才会替换 `data/market.json`。如果数据源临时失败，不会用空数据覆盖已有文件。
