#!/usr/bin/env python3
"""每日论文追踪数据生成器。

从 arXiv API 拉取最近 N 天 cs.CV / cs.RO 新论文，按四个方向的关键词对
标题/摘要打分，每方向取分最高的 K 篇；若 Hugging Face Daily Papers 可达，
用其社区点赞数作为热度加成（best-effort，失败不影响主流程）。

重要：所有字段（标题/作者/摘要/编号/日期）均直接来自 arXiv 元数据，
本脚本不做任何生成或改写 —— 保证"论文真实可靠"由构造保证。

输出 daily.js（window.DAILY = {...}），index.html 客户端渲染，file:// 下也能工作。
仅用标准库，无需 pip install。

用法: python3 tools/update_daily.py [--days 3] [--per-track 12]
"""
import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

ARXIV_API = "https://export.arxiv.org/api/query"
HF_DAILY = "https://huggingface.co/api/daily_papers"
NS = {"a": "http://www.w3.org/2005/Atom", "ar": "http://arxiv.org/schemas/atom"}
CATS_OK = {"cs.CV", "cs.RO"}

# (track_id, 名称, 关键词)。标题命中权重 3，摘要命中权重 1。
TRACKS = [
    ("slam", "SLAM 与状态估计", [
        "slam", "odometry", "visual-inertial", "lidar-inertial", "loop closure",
        "bundle adjustment", "factor graph", "vio", "lio", "visual localization",
        "pose estimation", "mapping", "kalman", "state estimation",
    ]),
    ("rep", "三维表示与重建", [
        "gaussian splatting", "splatting", "radiance field", "nerf",
        "novel view synthesis", "view synthesis", "surface reconstruction",
        "3d reconstruction", "mesh", "point cloud", "scene representation",
    ]),
    ("ff", "前馈几何（VGGT 一系）", [
        "pointmap", "point map", "feed-forward", "vggt", "dust3r", "mast3r",
        "visual geometry", "structure from motion", "sfm", "camera pose",
        "relative pose", "two-view", "pose-free", "calibration-free",
        "3d perception", "multi-view",
    ]),
    ("wm", "世界模型", [
        "world model", "world simulation", "world simulator", "video generation",
        "video prediction", "video diffusion", "interactive environment",
        "generative environment", "embodied", "jepa", "physical ai",
        "autonomous driving", "end-to-end driving",
    ]),
]
TRACK_NAMES = {t[0]: t[1] for t in TRACKS}


def http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "spatial-atlas-daily/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def fetch_arxiv(days: int, batch: int = 200) -> list:
    """按提交时间倒序拉取 cs.CV/cs.RO 近 N 天论文。"""
    q = urllib.parse.quote("cat:cs.CV OR cat:cs.RO")
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out, seen, start = [], set(), 0
    while True:
        url = (f"{ARXIV_API}?search_query={q}&start={start}&max_results={batch}"
               f"&sortBy=submittedDate&sortOrder=descending")
        for attempt in range(3):
            try:
                data = http_get(url)
                break
            except Exception as e:
                if attempt == 2:
                    sys.exit(f"arXiv API 请求失败: {e}")
                time.sleep(3 * (attempt + 1))
        root = ET.fromstring(data)
        entries = root.findall("a:entry", NS)
        if not entries:
            break
        oldest = None
        for e in entries:
            pub = datetime.fromisoformat(e.findtext("a:published", "", NS).replace("Z", "+00:00"))
            oldest = pub if oldest is None or pub < oldest else oldest
            if pub < cutoff:
                continue
            aid_raw = e.findtext("a:id", "", NS)
            aid = aid_raw.rsplit("/abs/", 1)[-1]
            aid_no_v = re.sub(r"v\d+$", "", aid)
            if aid_no_v in seen:
                continue
            cats = {c.get("term") for c in e.findall("a:category", NS)}
            if not (cats & CATS_OK):
                continue
            seen.add(aid_no_v)
            out.append({
                "id": aid_no_v,
                "title": clean(e.findtext("a:title", "", NS)),
                "authors": [clean(a.findtext("a:name", "", NS)) for a in e.findall("a:author", NS)],
                "abstract": clean(e.findtext("a:summary", "", NS)),
                "published": pub.strftime("%Y-%m-%d"),
                "comment": clean(e.findtext("ar:comment", "", NS)) or None,
                "url": f"https://arxiv.org/abs/{aid_no_v}",
            })
        if oldest is not None and oldest < cutoff:
            break
        start += batch
        if start >= 1000:  # 安全上限
            break
        time.sleep(3)  # arXiv 礼貌间隔
    return out


def load_summaries(path: str) -> dict:
    """摘要缓存 {arxiv_id: {"summary": str, "model": str}}，跨运行复用。"""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def llm_summarize_batch(papers: list, model: str, token: str) -> dict:
    """调用 GitHub Models 为一批论文生成中文摘要。严格锚定摘要原文：
    只总结问题/方法/结果，不得引入摘要之外的信息。返回 {id: {"summary","model"}}。"""
    import os
    token = token or os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return {}
    joined = "\n\n".join(
        f"[{p['id']}] 标题: {p['title']}\n摘要: {p['abstract']}" for p in papers)
    body = json.dumps({
        "messages": [
            {"role": "system", "content":
                "你是学术论文摘要员。对给定的每篇 arXiv 论文，仅根据其摘要用中文写 2-3 句总结，"
                "依次说明：解决什么问题、方法核心、主要结果或贡献。严禁添加摘要之外的信息、"
                "不得评价或臆测。输出 JSON 对象，键为论文 id（如 2609.07497），值为总结字符串，"
                "不要输出其他内容。"},
            {"role": "user", "content": joined},
        ],
        "temperature": 0.2,
    }).encode()
    req = urllib.request.Request(
        f"https://models.github.ai/inference/chat/completions", data=body,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json",
                 "User-Agent": "spatial-atlas-daily/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.loads(r.read())
        content = resp["choices"][0]["message"]["content"]
        content = re.sub(r"^```(json)?|```$", "", content.strip(), flags=re.M).strip()
        parsed = json.loads(content)
        return {k: {"summary": clean(str(v)), "model": model}
                for k, v in parsed.items() if isinstance(v, str) and v.strip()}
    except Exception as e:
        print(f"  LLM 摘要失败（回退为显示英文摘要）: {e}")
        return {}


def summarize_missing(papers: list, cache: dict, model: str, batch: int = 8) -> dict:
    """为缓存中没有摘要的论文请求 LLM；含限速退避。"""
    need = [p for p in papers if p["id"] not in cache]
    for i in range(0, len(need), batch):
        chunk = need[i:i + batch]
        got = llm_summarize_batch(chunk, model, None)
        cache.update(got)
        print(f"  LLM 摘要 {min(i + batch, len(need))}/{len(need)}")
        if not got:  # 连续失败则停止，避免刷爆限额
            break
        time.sleep(2)
    return cache


def fetch_hf_upvotes() -> dict:
    """HF Daily Papers 点赞数：{arxiv_id: upvotes}。失败返回空。"""
    try:
        data = json.loads(http_get(HF_DAILY, timeout=20))
        return {item["paper"]["id"].split("v")[0]: int(item["paper"].get("upvotes", 0))
                for item in data if "paper" in item}
    except Exception:
        return {}


def score_paper(p: dict) -> dict:
    """返回 (best_track, score)。标题命中 3 分/词，摘要 1 分/词（每词只计一次）。"""
    title, abstract = p["title"].lower(), p["abstract"].lower()
    best, best_score = None, 0
    for tid, _, kws in TRACKS:
        s = 0
        for kw in kws:
            if kw in title:
                s += 3
            elif kw in abstract:
                s += 1
        if s > best_score:
            best, best_score = tid, s
    return best, best_score


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3, help="回溯天数（覆盖周末）")
    ap.add_argument("--per-track", type=int, default=12, help="每方向保留篇数")
    ap.add_argument("--min-score", type=int, default=3, help="入选最低分")
    ap.add_argument("--out", default="daily.js")
    ap.add_argument("--summaries", default="summaries.json", help="摘要缓存文件")
    ap.add_argument("--llm", action="store_true",
                    help="为缓存中缺失的论文调用 GitHub Models 生成摘要（CI 用）")
    ap.add_argument("--model", default="openai/gpt-4.1-mini", help="GitHub Models 模型名")
    args = ap.parse_args()

    print(f"拉取 arXiv 近 {args.days} 天 cs.CV/cs.RO 论文 …")
    papers = fetch_arxiv(args.days)
    print(f"  共 {len(papers)} 篇")
    upv = fetch_hf_upvotes()
    if upv:
        print(f"  HF Daily Papers 热度表: {len(upv)} 条")

    by_track = {tid: [] for tid, _, _ in TRACKS}
    selected = []
    for p in papers:
        tid, s = score_paper(p)
        if tid is None or s < args.min_score:
            continue
        s += upv.get(p["id"], 0) / 10  # 热度加成：10 赞 ≈ 1 分
        selected.append(dict(p, track=tid, score=round(s, 2)))
        by_track[tid].append(selected[-1])

    # 中文摘要：优先用缓存；--llm 时为缺失项调用 GitHub Models（失败则无摘要，前端回退英文摘要）
    cache = load_summaries(args.summaries)
    if args.llm:
        cache = summarize_missing(selected, cache, args.model)
    for p in selected:
        if p["id"] in cache:
            p["summary"] = cache[p["id"]]["summary"]

    tracks_out = {}
    total = 0
    for tid, name, _ in TRACKS:
        lst = sorted(by_track[tid], key=lambda x: (-x["score"], x["published"]), reverse=False)
        lst = sorted(lst, key=lambda x: -x["score"])[: args.per_track]
        for p in lst:
            p.pop("score")
            p.pop("track")
        tracks_out[tid] = {"name": name, "papers": lst}
        total += len(lst)
        n_sum = sum(1 for p in lst if "summary" in p)
        print(f"  [{tid}] {name}: {len(lst)} 篇（含中文摘要 {n_sum}）")

    now = datetime.now(timezone.utc)
    daily = {
        "generated_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "date_cn": now.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d"),
        "window_days": args.days,
        "source": "arXiv API (cs.CV/cs.RO) · 热度加成: Hugging Face Daily Papers",
        "note": "所有条目直接来自 arXiv 元数据，未经人工或模型改写；预印本未经同行评审。",
        "tracks": tracks_out,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("window.DAILY = " + json.dumps(daily, ensure_ascii=False) + ";\n")
    with open(args.summaries, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    print(f"写入 {args.out}: 共 {total} 篇 · 数据日期 {daily['date_cn']}")


if __name__ == "__main__":
    main()
