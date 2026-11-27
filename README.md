# 五日筹码

A股五日筹码可视化页面，数据由 GitHub Actions 自动更新，不依赖本机。

- 线上：<https://yjj798.github.io/xgrs-chip-5d/>
- 页面已设置访问密码，首次进入需输入；密码只用于前端访问门，不替代服务端鉴权。
- 页面为独立静态 HTML，包含筛选、排序、分页、概念折叠和概念点击筛选，不依赖后端。
- 概念排行与表格“概念”列的名称都可点击：只显示该主概念的股票；再次点击同一名称或点工具栏“概念 ×”即可清除。

## 自动更新

`.github/workflows/update.yml` 每个工作日北京时间 16:10 运行：

1. `scripts/fetch_limitup_cloud.py` 用同花顺涨停池接口生成当日 `data/<日期>/01_stocks_raw_<日期>.json`；
2. `scripts/update_pool_db.py` 用同花顺日 K + 腾讯盘后快照给 `data/pool_quant.db` 追加新交易日，不改已有历史；
3. `scripts/chip_6d_html.py` 重算最近 6 个交易日的筹码分布，写 `data/chip_6d_data.json`；
4. `scripts/chip_5d_14cols.py` 生成 `index.html`；
5. Actions 自动提交并推送，GitHub Pages 重建。

也可以在 Actions 页面手动触发，并指定交易日 `YYYY-MM-DD`。

## 本地运行

```powershell
$env:XGRS_BASE = "D:\ai\xgrs\deploy_github"
python scripts\update_cloud.py --date 2026-11-27
```

`data/pool_quant.db` 是从本机 `quant_v2.db` 导出的 391 只股票精简库（约 9.4MB），保留全部历史，已去掉非股票池数据。

数据仅用于展示，不构成投资建议。
