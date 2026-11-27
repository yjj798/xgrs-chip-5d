# 五日筹码

A股五日筹码可视化页面，数据截至 **2026-11-27**。

- 直接打开：`index.html`
- 线上：<https://yjj798.github.io/xgrs-chip-5d/>
- 页面为独立静态 HTML，包含筛选、排序、分页、概念折叠和概念点击筛选，不依赖后端。
- 概念排行与表格“概念”列的名称都可点击：只显示该主概念的股票；再次点击同一名称或点工具栏“概念 ×”即可清除。

## 数据更新

页面是生成时快照，数据不会自动刷新。更新步骤：

```powershell
python scripts\chip_6d_html.py
python scripts\chip_5d_14cols.py
```

然后把 `reports\五日筹码_表头排序_6日_50只一页.html` 复制为部署仓库的 `index.html`，再执行：

```powershell
powershell -File D:\ai\xgrs\scripts\deploy_github_pages.ps1
```

脚本会读取本机账号库里的 GitHub token，提交 `index.html` / `README.md` / `.nojekyll` 并触发 Pages 重建。

数据仅用于展示，不构成投资建议。
