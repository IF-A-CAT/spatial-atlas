#!/usr/bin/env python3
"""每日论文追踪数据生成器。

从 arXiv 拉取最近 N 天 cs.CV / cs.RO 新论文，按四个方向的关键词对
标题/摘要打分，每方向取分最高的 K 篇；若 Hugging Face Daily Papers 可达，
用其社区点赞数作为热度加成（best-effort，失败不影响主流程）。

数据源两个，按序尝试：
  1. OAI-PMH（oaipmh.arxiv.org，arXiv 官方推荐的批量抓取端点，独立限速桶）
  2. Atom API（export.arxiv.org/api/query，历史上被限流的主力）
GitHub Actions 的共享数据中心 IP 常被 Atom 端点以 406/429 挡回，而 OAI-PMH 通常可达，
故把它放在前面。两个都失败时保留上一份 daily.js 不覆盖，避免站点被清空。

重要：所有字段（标题/作者/摘要/编号/日期）均直接来自 arXiv 元数据，
本脚本不做任何生成或改写 —— 保证"论文真实可靠"由构造保证。

输出 daily.js（window.DAILY = {...}），index.html 客户端渲染，file:// 下也能工作。
仅用标准库，无需 pip install。

用法: python3 tools/update_daily.py [--days 3] [--per-track 12]
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

ARXIV_API = "https://export.arxiv.org/api/query"
OAI_PMH = "https://oaipmh.arxiv.org/oai"
HF_DAILY = "https://huggingface.co/api/daily_papers"
NS = {"a": "http://www.w3.org/2005/Atom", "ar": "http://arxiv.org/schemas/atom"}
OAI_NS = {"oai": "http://www.openarchives.org/OAI/2.0/", "arx": "http://arxiv.org/OAI/arXiv/"}
CATS_OK = {"cs.CV", "cs.RO"}
# arXiv 要求 UA 能标识客户端；带仓库地址便于对方在误封时联系放行。
UA = "spatial-atlas-daily/1.0 (+https://github.com/IF-A-CAT/spatial-atlas)"
STALE_DAYS = 5  # 现有 daily.js 超过这么多天没更新则视为真故障，任务报错


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


def http_get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_with_retry(url: str, label: str, attempts: int = 5, timeout: int = 60) -> bytes:
    """抓一次，失败按指数退避重试。arXiv 对数据中心 IP 间歇性返回 406/429/503，
    这类拒绝是临时的，退避后往往能过；429/503 若带 Retry-After 则优先遵守。

    失败时把最后几次的具体错误码带进异常消息 —— 日志里只写"请求失败"没法定位。"""
    errors = []
    for i in range(attempts):
        try:
            return http_get(url, timeout=timeout)
        except urllib.error.HTTPError as e:
            wait = _retry_after(e) or 5 * 2 ** i
            errors.append(f"HTTP {e.code} {e.reason}")
            print(f"  [{label}] HTTP {e.code} {e.reason}，{wait}s 后重试（{i + 1}/{attempts}）"
                  f"{_http_error_detail(e)}", flush=True)
        except Exception as e:
            wait = 5 * 2 ** i
            errors.append(f"{type(e).__name__}: {e}")
            print(f"  [{label}] {type(e).__name__}: {e}，{wait}s 后重试（{i + 1}/{attempts}）",
                  flush=True)
        if i < attempts - 1:
            time.sleep(wait)
    tail = "；".join(errors[-3:])
    raise RuntimeError(f"{label} 连续 {attempts} 次失败（最近: {tail}）")


def _retry_after(e) -> int:
    try:
        return max(1, min(120, int(e.headers.get("Retry-After", ""))))
    except Exception:
        return 0


def _http_error_detail(e) -> str:
    """把 4xx/5xx 的响应头与正文片段带出来。arXiv 的 406 到底是 WAF 拒了还是
    限速，只看状态码分不清，正文/响应头里才有线索（对排查 CI 上的失败尤其关键）。"""
    detail = []
    for h in ("Retry-After", "Server", "X-Cache", "Via", "X-Served-By", "Content-Type"):
        v = e.headers.get(h) if e.headers else None
        if v:
            detail.append(f"{h}={v}")
    try:
        body = clean(e.read().decode("utf-8", "replace"))[:200]
    except Exception:
        body = ""
    if body:
        detail.append(f"body={body!r}")
    return ("  [" + " · ".join(detail) + "]") if detail else ""


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def fetch_oai(days: int, max_pages: int = 12) -> list:
    """OAI-PMH 抓取。arXiv 官方推荐的批量路径，走独立限速桶，Actions 上比 Atom 稳。

    注意 OAI-PMH 的 datestamp 是"记录被更新/公告"的日期，被修回的老论文也会出现，
    因此按窗口拉取后再用 created（v1 提交日）过滤，只留窗口内的新文。"""
    today = datetime.now(timezone.utc).date()
    cutoff = today - timedelta(days=days)
    url = (f"{OAI_PMH}?verb=ListRecords&metadataPrefix=arXiv&set=cs"
           f"&from={(cutoff - timedelta(days=2)).isoformat()}&until={today.isoformat()}")
    out, seen, page = [], set(), 0
    while True:
        root = ET.fromstring(fetch_with_retry(url, "OAI-PMH", attempts=3, timeout=120))
        lr = root.find("oai:ListRecords", OAI_NS)
        if lr is None:
            err = root.find("oai:error", OAI_NS)
            raise RuntimeError(f"OAI-PMH 返回错误: {clean(err.text) if err is not None else '无 ListRecords'}")
        for rec in lr.findall("oai:record", OAI_NS):
            md = rec.find("oai:metadata/arx:arXiv", OAI_NS)
            if md is None:  # 已删除记录 / 只有 header
                continue
            aid = clean(md.findtext("arx:id", "", OAI_NS))
            if not aid or aid in seen:
                continue
            if not (set((md.findtext("arx:categories", "", OAI_NS) or "").split()) & CATS_OK):
                continue
            created = clean(md.findtext("arx:created", "", OAI_NS))[:10]
            try:
                if datetime.fromisoformat(created).date() < cutoff:
                    continue
            except ValueError:
                continue
            seen.add(aid)
            authors = []
            for a in md.findall("arx:authors/arx:author", OAI_NS):
                name = clean(f"{a.findtext('arx:forenames', '', OAI_NS) or ''} "
                             f"{a.findtext('arx:keyname', '', OAI_NS) or ''}")
                if name:
                    authors.append(name)
            out.append({
                "id": aid,
                "title": clean(md.findtext("arx:title", "", OAI_NS)),
                "authors": authors,
                "abstract": clean(md.findtext("arx:abstract", "", OAI_NS)),
                "published": created,
                "comment": clean(md.findtext("arx:comments", "", OAI_NS)) or None,
                "url": f"https://arxiv.org/abs/{aid}",
            })
        page += 1
        tok = lr.find("oai:resumptionToken", OAI_NS)
        token = (tok.text or "").strip() if tok is not None else ""
        if not token or page >= max_pages:
            break
        url = f"{OAI_PMH}?verb=ListRecords&resumptionToken={token}"
        time.sleep(4)  # arXiv 礼貌间隔
    return out


RSS_CATS = ("cs.CV", "cs.RO")
RSS_NS = {"arxiv": "http://arxiv.org/schemas/atom", "dc": "http://purl.org/dc/elements/1.1/"}
# RSS 的 description 形如 "arXiv:2609.26809v1 Announce Type: new \nAbstract: ..."
RSS_DESC = re.compile(r"^arXiv:(?P<id>\S+?)(?:v\d+)?\s+Announce Type:\s*(?P<kind>\w+)\s*Abstract:\s*(?P<abs>.*)$",
                      re.S)


def fetch_rss(days: int = 1) -> list:
    """arXiv 官方 RSS：每个类目一份，只含最近一次公告的新论文，载荷小、响应快。

    只覆盖当天公告（arXiv 周日到周四公告），所以它保证"今天一定有东西"，
    但补不了更早的窗口 —— 这是 OAI-PMH 被限流时的降级档，不是替代品。
    replace（旧文修订）不算新论文，排除。"""
    out, seen = [], set()
    for cat in RSS_CATS:
        root = ET.fromstring(fetch_with_retry(f"https://rss.arxiv.org/rss/{cat}",
                                              f"RSS {cat}", attempts=3, timeout=45))
        for item in root.findall("channel/item"):
            m = RSS_DESC.match(clean(item.findtext("description", "")))
            if not m or m.group("kind") == "replace":
                continue
            aid = re.sub(r"v\d+$", "", m.group("id"))
            if aid in seen:
                continue
            seen.add(aid)
            creators = clean(item.findtext("dc:creator", "", RSS_NS))
            pub = item.findtext("pubDate", "")
            try:
                day = datetime.strptime(pub[:16], "%a, %d %b %Y").strftime("%Y-%m-%d")
            except ValueError:
                day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            out.append({
                "id": aid,
                "title": clean(item.findtext("title", "")),
                "authors": [clean(a) for a in creators.split(",") if clean(a)],
                "abstract": clean(m.group("abs")),
                "published": day,
                "comment": None,  # RSS 不含 comment 字段
                "url": f"https://arxiv.org/abs/{aid}",
            })
        time.sleep(3)
    return out


def fetch_arxiv_atom(days: int, batch: int = 200) -> list:
    """按提交时间倒序拉取 cs.CV/cs.RO 近 N 天论文（Atom API，备用源）。"""
    q = urllib.parse.quote("cat:cs.CV OR cat:cs.RO")
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out, seen, start = [], set(), 0
    while True:
        url = (f"{ARXIV_API}?search_query={q}&start={start}&max_results={batch}"
               f"&sortBy=submittedDate&sortOrder=descending")
        data = fetch_with_retry(url, "Atom", timeout=90)
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
        time.sleep(5)  # arXiv 礼貌间隔（2026 起为强制限速）
    return out


SOURCES = (("OAI-PMH", fetch_oai), ("RSS", fetch_rss), ("Atom", fetch_arxiv_atom))


def fetch_papers(days: int):
    """按序尝试所有数据源，全部失败则冷却后再来一轮 —— arXiv 的 406/429 多是短时封锁。

    返回 (papers, 诊断行)。诊断行同时进日志与 GitHub step summary，
    这样失败原因在 Actions 页面上直接可见，不必翻日志。"""
    notes = []
    for rnd, cooldown in enumerate((0, 90), start=1):
        if cooldown:
            print(f"  数据源均不可达，等待 {cooldown}s 冷却后重试（第 {rnd} 轮）", flush=True)
            time.sleep(cooldown)
        for name, fn in SOURCES:
            try:
                papers = fn(days)
            except Exception as e:
                notes.append(f"{name}（第 {rnd} 轮）: {e}")
                print(f"::warning::抓取失败 — {name}（第 {rnd} 轮）: {e}", flush=True)
                continue
            if papers:
                print(f"  {name} 取到 {len(papers)} 篇", flush=True)
                notes.append(f"{name}: 成功，{len(papers)} 篇")
                return papers, notes
            notes.append(f"{name}（第 {rnd} 轮）: 返回 0 篇")
    return [], notes


def write_step_summary(lines: list) -> None:
    """写 GitHub Actions 运行摘要；本地跑时静默跳过。"""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass


def daily_age_days(path: str):
    """现有 daily.js 的生成时间距今多少天；读不到返回 None。"""
    try:
        with open(path, encoding="utf-8") as f:
            m = re.search(r'"generated_utc":\s*"([^"]+)"', f.read(600))
        gen = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - gen).total_seconds() / 86400
    except Exception:
        return None


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
    papers, notes = fetch_papers(args.days)
    if not papers:
        # 全部数据源不可达：保留上一份 daily.js，不覆盖、不提交。
        # 短时抖动不该天天炸红叉；但连续多日没成功就是真故障，需要人看。
        age = daily_age_days(args.out)
        write_step_summary(["## 每日论文抓取失败", ""] + [f"- {n}" for n in notes])
        if age is not None and age <= STALE_DAYS:
            print(f"::warning::数据源全部不可达，保留 {args.out}（{age:.1f} 天前生成），本次不更新")
            return
        sys.exit(f"数据源全部不可达，且 {args.out} 已过期或缺失，需人工介入")
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
        "source": "arXiv OAI-PMH/API (cs.CV/cs.RO) · 热度加成: Hugging Face Daily Papers",
        "note": "所有条目直接来自 arXiv 元数据，未经人工或模型改写；预印本未经同行评审。",
        "tracks": tracks_out,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("window.DAILY = " + json.dumps(daily, ensure_ascii=False) + ";\n")
    with open(args.summaries, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    print(f"写入 {args.out}: 共 {total} 篇 · 数据日期 {daily['date_cn']}")
    write_step_summary([
        f"## 每日论文更新 {daily['date_cn']}",
        "",
        f"- 数据源: {notes[-1]}",
        f"- 入选 {total} 篇，中文摘要 {sum(1 for t in tracks_out.values() for p in t['papers'] if 'summary' in p)} 篇",
    ] + [f"- {n}" for n in notes[:-1]])


if __name__ == "__main__":
    main()
