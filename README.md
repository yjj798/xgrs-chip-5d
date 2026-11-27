# 五日筹码

A股五日筹码可视化页面，数据截至 **2026-11-27**。

- 直接打开：`index.html`
- GitHub Pages：仓库启用 Pages 后访问 `https://<用户名>.github.io/<仓库名>/`
- 页面为独立静态 HTML，包含筛选、排序、分页和概念折叠面板，不依赖后端。

## 数据更新

页面是生成时快照，数据不会自动刷新。更新步骤：

```powershell
python scripts\chip_6d_html.py
python scripts\chip_5d_14cols.py
```

然后把 `reports\五日筹码_表头排序_6日_50只一页.html` 复制为部署仓库的 `index.html` 并提交。

数据仅用于展示，不构成投资建议。
