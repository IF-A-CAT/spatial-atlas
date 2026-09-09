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
    args = ap.parse_args()

    print(f"拉取 arXiv 近 {args.days} 天 cs.CV/cs.RO 论文 …")
    papers = fetch_arxiv(args.days)
    print(f"  共 {len(papers)} 篇")
    upv = fetch_hf_upvotes()
    if upv:
        print(f"  HF Daily Papers 热度表: {len(upv)} 条")

    by_track = {tid: [] for tid, _, _ in TRACKS}
    for p in papers:
        tid, s = score_paper(p)
        if tid is None or s < args.min_score:
            continue
        s += upv.get(p["id"], 0) / 10  # 热度加成：10 赞 ≈ 1 分
        p2 = dict(p, track=tid, score=round(s, 2))
        by_track[tid].append(p2)

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
        print(f"  [{tid}] {name}: {len(lst)} 篇")

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
    print(f"写入 {args.out}: 共 {total} 篇 · 数据日期 {daily['date_cn']}")


if __name__ == "__main__":
    main()
