# 空间智能研究图谱 · Spatial Atlas

SLAM / 3D 重建 / VGGT 前馈几何 / 世界模型四个方向的**每日更新论文追踪站** +
**人工核验的高影响力精选图谱**，并梳理各方向最新进展与相互联系。

## 两层内容结构

| 层 | 数据来源 | 更新方式 | 真实性保证 |
| --- | --- | --- | --- |
| **每日追踪** | arXiv API（cs.CV / cs.RO）| GitHub Actions 每日 09:23 / 21:23（北京）自动拉取打分，写 `daily.js` 后自动提交 | 条目直接取自 arXiv 元数据，**零生成**，标题可点击溯源 |
| **精选图谱**（40 篇） | 人工筛选 + 逐篇核验 | 手动维护 `index.html` 内 `PAPERS` 数组 | 标题/作者/venue/编号对照 arXiv 与官方页面核验 |

每日追踪按四方向关键词对标题（权重 3）/ 摘要（权重 1）打分，每方向取前 12 篇；
Hugging Face Daily Papers 点赞数作为热度加成（best-effort，失败不影响）。
预印本未经同行评审，已在页面明确标注。

## 调整每日追踪

- 关键词 / 方向：改 `tools/update_daily.py` 里 `TRACKS`
- 篇数 / 回溯天数 / 分数阈值：`--per-track / --days / --min-score`
- 更新频率：`.github/workflows/update-daily.yml` 里 cron（UTC）
- 手动触发：GitHub 仓库 → Actions → daily-papers → Run workflow

## 收录标准（精选图谱）

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
