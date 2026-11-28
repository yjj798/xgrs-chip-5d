<p align="center"><img src="logo.svg" alt="牛逼选股" width="360"></p>

<h1 align="center">牛逼选股</h1>

A股五日筹码可视化页面，数据由 GitHub Actions 更新，不依赖本机。

- 线上：<https://t.legaga.top/>
- 选股口径：**仅主板 · 非ST · 最新价 ≤ 8 元 · 流通市值 < 200 亿**（页面脚注同步声明；市值列为流通市值）
- 页面为独立静态 HTML（数据内嵌），含筛选、排序、分页、概念折叠/筛选。
- 页面口令为前端访问门（加盐 PBKDF2），不替代服务端鉴权；底部"最新更新"按钮经 Worker <https://up.legaga.top> 触发云端更新，需要更新口令（`X-Update-Key`）。

## 自动更新

`.github/workflows/update.yml` 计划每个工作日按北京时间运行三次：

- `09:35`：盘前/开盘后刷新（盘中模式，仅生成 `.cache` 产物，不发布）
- `12:00`：午间刷新（盘中模式，仅生成 `.cache` 产物，不发布）
- `16:00`：收盘最终更新（收盘模式，唯一会提交 `index.html` 并回写状态库的档位）

> 首次自动触发待验证：仓库与工作流于 2026-11-27 晚间上线，首个 cron 窗口为 2026-11-30（周一）。在此之前的页面版本均来自手动触发。

工作日遇 `holiday_list.json` 中的法定节假日直接跳过。

盘中模式只把腾讯当日快照写入临时库 `.cache/pool_quant_intraday.db`，页面等产物输出到 `.cache/`；**不写正式库、不提交仓库**。收盘模式才把最终 OHLCV 写入正式库，并生成、提交 `index.html`。

1. `scripts/fetch_limitup_cloud.py` 用同花顺涨停池接口生成/刷新当日 `data/<日期>/01_stocks_raw_<日期>.json`；盘中空结果不覆盖已有文件；
2. `scripts/update_pool_db.py` 盘中用 `--intraday` 更新临时库；收盘用同花顺日 K + 腾讯盘后快照更新正式库（仅主板池）；
3. `scripts/chip_6d_html.py` 重算最近 6 个交易日的筹码分布，写 `data/chip_6d_data.json`（不入仓库）；
4. `scripts/chip_5d_14cols.py` 生成 `index.html`（含板块白名单硬校验：非主板直接失败）；
5. Actions 提交 `index.html` 与少量公开行情数据并推送，GitHub Pages 重建。

## 状态库与数据资产

- 正式库 `data/pool_quant.db` 不再提交到仓库，由 GitHub Release `state` 的资产承载：工作流开始前下载、收盘后回写。
- `data/chip_6d_data.json` 为中间产物，不入仓库。
- `.gitignore` 已排除上述文件与 `.cache/`、`reports/`。

## 本地运行

```powershell
$env:XGRS_BASE = "D:\ai\xgrs\deploy_github"
python scripts\update_cloud.py --date 2026-11-27
```

也可以在 Actions 页面手动触发，并指定交易日 `YYYY-MM-DD` 和模式 `close` / `intraday`。

数据仅用于展示，不构成投资建议。
