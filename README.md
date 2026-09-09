# 空间智能研究图谱 · Spatial Atlas

精选 SLAM / 3D 重建 / VGGT 前馈几何 / 世界模型四个方向的**高影响力论文图谱**，
梳理各方向最新进展与相互联系。

## 收录标准

- 只收录可验证的论文：每篇均附 arXiv 编号或项目主页，标题、作者、发表 venue 均经核实。
- 只收录有影响力的课题组 / 高校 / 实验室（HKU MARS、ETH、Oxford VGG、TUM、Inria、
  DeepMind、Meta FAIR、NVIDIA、浙大、上交、清华等）或顶级 venue
  （CVPR、ICCV、ECCV、NeurIPS、ICML、ICLR、TRO、ICRA、RSS、TPAMI、Nature 等）的工作。

## 本地预览

纯静态站点，无构建步骤，直接打开 `index.html` 或：

```bash
python3 -m http.server 8000
# 浏览器打开 http://localhost:8000
```

## 发布到 GitHub Pages

```bash
cd spatial-atlas
git init -b main
git add -A
git commit -m "feat: initial paper atlas site"
git remote add origin https://github.com/IF-A-CAT/spatial-atlas.git
git push -u origin main
```

然后在 GitHub 仓库页面：**Settings → Pages → Build and deployment →
Source 选 “Deploy from a branch”，Branch 选 `main` / `(root)` → Save**。

一两分钟后访问：

```
https://if-a-cat.github.io/spatial-atlas/
```

## 维护

论文数据集中在 `index.html` 内的 `PAPERS` 数组中（每篇一个 JS 对象），
按同样字段增删即可，页面自动渲染、自动更新筛选与统计。
