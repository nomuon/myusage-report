#!/usr/bin/env python3
"""
claude_usage_report.py
Claude Code ローカル使用状況レポート生成ツール
外部API・ネットワーク通信なし / 完全ローカル処理
"""

import json
import os
import re
import sys
import math
import argparse
from pathlib import Path
from datetime import datetime, timedelta, timezone
from collections import defaultdict, Counter

# ─────────────────────────────────────────────
# コスト計算用料金レート (USD / 1M tokens)
# ─────────────────────────────────────────────

MODEL_PRICING = {
    "claude-opus-4": {
        "input": 15.0, "output": 75.0,
        "cache_create_mul": 1.25, "cache_read_mul": 0.10,
    },
    "claude-sonnet-4": {
        "input": 3.0, "output": 15.0,
        "cache_create_mul": 1.25, "cache_read_mul": 0.10,
    },
    "claude-haiku-4": {
        "input": 0.80, "output": 4.0,
        "cache_create_mul": 1.25, "cache_read_mul": 0.10,
    },
}
DEFAULT_PRICING = MODEL_PRICING["claude-sonnet-4"]

def get_model_pricing(model_name: str) -> dict:
    for key, pricing in MODEL_PRICING.items():
        if key in model_name:
            return pricing
    return DEFAULT_PRICING

def format_tokens(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)

# ─────────────────────────────────────────────
# データ解析
# ─────────────────────────────────────────────

CATEGORY_PATTERNS = {
    "デバッグ・バグ修正": [
        "bug", "fix", "error", "exception", "crash", "debug", "issue", "fail",
        "バグ", "エラー", "修正", "失敗", "例外", "デバッグ"
    ],
    "コード生成・実装": [
        "implement", "create", "build", "write", "generate", "add feature",
        "実装", "作成", "構築", "追加", "生成", "コード書いて"
    ],
    "リファクタリング": [
        "refactor", "cleanup", "clean up", "restructure", "reorganize", "optimize",
        "リファクタ", "整理", "最適化", "改善", "リストラクチャ"
    ],
    "レビュー・説明": [
        "explain", "review", "what is", "how does", "understand", "describe",
        "説明", "レビュー", "教えて", "とは", "どうやって", "解説"
    ],
    "インフラ・設定": [
        "docker", "aws", "deploy", "config", "setup", "install", "server",
        "nginx", "terraform", "ci/cd", "github actions", "環境構築", "デプロイ", "設定"
    ],
    "テスト": [
        "test", "spec", "jest", "vitest", "unittest", "assert", "mock",
        "テスト", "スペック"
    ],
    "ドキュメント・計画": [
        "document", "readme", "plan", "design", "architecture", "spec",
        "ドキュメント", "設計", "計画", "アーキテクチャ"
    ],
}

def categorize_session(messages):
    text = " ".join(
        (m.get("content") or "") if isinstance(m.get("content"), str)
        else " ".join(
            b.get("text", "") for b in (m.get("content") or [])
            if isinstance(b, dict) and b.get("type") == "text"
        )
        for m in messages[:4]
    ).lower()
    
    scores = {}
    for cat, keywords in CATEGORY_PATTERNS.items():
        scores[cat] = sum(1 for kw in keywords if kw in text)
    
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "その他"

def classify_session_depth(msg_count):
    if msg_count <= 2:
        return "クイック (1-2)"
    elif msg_count <= 6:
        return "標準 (3-6)"
    elif msg_count <= 14:
        return "深掘り (7-14)"
    else:
        return "集中セッション (15+)"

def parse_projects(claude_dir: Path, days: int = 0):
    projects_dir = claude_dir / "projects"
    if not projects_dir.exists():
        return []

    sessions = []
    cutoff = None
    if days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    for project_dir in sorted(projects_dir.iterdir()):
        if not project_dir.is_dir():
            continue
        project_name = project_dir.name
        # decode URL-encoded path
        try:
            decoded = project_name.replace("-", "/")
        except Exception:
            decoded = project_name

        for jsonl_file in sorted(project_dir.glob("*.jsonl")):
            messages = []
            first_ts = None
            last_ts = None
            models_seen = set()
            token_data = {}
            tool_uses = Counter()

            try:
                with open(jsonl_file, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        ts_str = entry.get("timestamp")
                        ts = None
                        if ts_str:
                            try:
                                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                            except ValueError:
                                pass

                        if ts:
                            if first_ts is None or ts < first_ts:
                                first_ts = ts
                            if last_ts is None or ts > last_ts:
                                last_ts = ts

                        msg = entry.get("message", {})
                        role = msg.get("role") if msg else entry.get("type")
                        content = msg.get("content", "") if msg else ""
                        model = msg.get("model", "") if msg else ""

                        if model:
                            models_seen.add(model)

                        # token usage extraction (deduplicate by message id)
                        if msg:
                            msg_id = msg.get("id", "")
                            usage = msg.get("usage", {})
                            if msg_id and usage:
                                if msg_id not in token_data:
                                    token_data[msg_id] = {
                                        "model": model or "",
                                        "input": 0, "output": 0,
                                        "cache_create": 0, "cache_read": 0,
                                    }
                                td = token_data[msg_id]
                                td["input"] = max(td["input"], usage.get("input_tokens", 0))
                                td["output"] = max(td["output"], usage.get("output_tokens", 0))
                                td["cache_create"] = max(td["cache_create"], usage.get("cache_creation_input_tokens", 0))
                                td["cache_read"] = max(td["cache_read"], usage.get("cache_read_input_tokens", 0))
                                if model:
                                    td["model"] = model

                        # tool_use extraction
                        if isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict) and block.get("type") == "tool_use":
                                    tool_uses[block.get("name", "unknown")] += 1

                        if role in ("user", "assistant"):
                            messages.append({"role": role, "content": content, "ts": ts})

            except (IOError, OSError):
                continue

            if not messages:
                continue

            if cutoff and first_ts and first_ts < cutoff:
                continue

            user_msgs = [m for m in messages if m["role"] == "user"]
            asst_msgs = [m for m in messages if m["role"] == "assistant"]

            category = categorize_session(user_msgs)
            depth_label = classify_session_depth(len(messages))

            # estimate total chars
            total_chars = sum(
                len(m["content"]) if isinstance(m["content"], str)
                else sum(
                    len(b.get("text", "")) for b in (m["content"] or [])
                    if isinstance(b, dict)
                )
                for m in messages
            )

            # token & cost aggregation
            total_input_tokens = sum(td["input"] for td in token_data.values())
            total_output_tokens = sum(td["output"] for td in token_data.values())
            total_cache_creation = sum(td["cache_create"] for td in token_data.values())
            total_cache_read = sum(td["cache_read"] for td in token_data.values())

            session_cost = 0.0
            cost_without_cache = 0.0
            model_costs = defaultdict(float)
            for td in token_data.values():
                pricing = get_model_pricing(td["model"]) if td["model"] else DEFAULT_PRICING
                input_rate = pricing["input"] / 1_000_000
                output_rate = pricing["output"] / 1_000_000
                cache_create_rate = input_rate * pricing["cache_create_mul"]
                cache_read_rate = input_rate * pricing["cache_read_mul"]

                cost = (
                    td["input"] * input_rate
                    + td["output"] * output_rate
                    + td["cache_create"] * cache_create_rate
                    + td["cache_read"] * cache_read_rate
                )
                session_cost += cost

                no_cache_cost = (
                    (td["input"] + td["cache_create"] + td["cache_read"]) * input_rate
                    + td["output"] * output_rate
                )
                cost_without_cache += no_cache_cost

                short_model = re.sub(r"-\d{8}$", "", td["model"]) if td["model"] else "(unknown)"
                model_costs[short_model] += cost

            sessions.append({
                "project": decoded,
                "file": jsonl_file.name,
                "first_ts": first_ts,
                "last_ts": last_ts,
                "total_messages": len(messages),
                "user_messages": len(user_msgs),
                "assistant_messages": len(asst_msgs),
                "models": list(models_seen),
                "category": category,
                "depth": depth_label,
                "total_chars": total_chars,
                "duration_min": (
                    round((last_ts - first_ts).total_seconds() / 60, 1)
                    if first_ts and last_ts else 0
                ),
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
                "cache_creation_tokens": total_cache_creation,
                "cache_read_tokens": total_cache_read,
                "estimated_cost": round(session_cost, 4),
                "cost_without_cache": round(cost_without_cache, 4),
                "model_costs": dict(model_costs),
                "tool_uses": dict(tool_uses),
            })

    return sorted(sessions, key=lambda s: s["first_ts"] or datetime.min.replace(tzinfo=timezone.utc))

def compute_stats(sessions):
    if not sessions:
        return {}

    all_ts = [s["first_ts"] for s in sessions if s["first_ts"]]
    if not all_ts:
        return {}

    date_min = min(all_ts).date()
    date_max = max(all_ts).date()

    # hourly heatmap
    hour_counts = Counter(ts.hour for ts in all_ts)
    hourly = [hour_counts.get(h, 0) for h in range(24)]

    # day of week (月=0 … 日=6)
    dow_counts = Counter(ts.weekday() for ts in all_ts)
    dow_labels = ["月", "火", "水", "木", "金", "土", "日"]
    dow_data = [dow_counts.get(i, 0) for i in range(7)]

    # daily activity (last 90 days)
    daily_counts = Counter(ts.date() for ts in all_ts)
    today = datetime.now(timezone.utc).date()
    start_day = today - timedelta(days=89)
    daily_labels = []
    daily_values = []
    d = start_day
    while d <= today:
        daily_labels.append(d.isoformat())
        daily_values.append(daily_counts.get(d, 0))
        d += timedelta(days=1)

    # models
    model_counts = Counter()
    for s in sessions:
        for m in s["models"]:
            short = re.sub(r"-\d{8}$", "", m)  # remove date suffix
            model_counts[short] += 1
    if not model_counts:
        model_counts["(不明)"] = len(sessions)

    # categories
    cat_counts = Counter(s["category"] for s in sessions)

    # depth distribution
    depth_counts = Counter(s["depth"] for s in sessions)
    depth_order = ["クイック (1-2)", "標準 (3-6)", "深掘り (7-14)", "集中セッション (15+)"]
    depth_data = [depth_counts.get(d, 0) for d in depth_order]

    # top projects
    proj_counts = Counter(s["project"] for s in sessions)
    top_projects = proj_counts.most_common(10)

    # weekly trend (last 12 weeks)
    week_counts = Counter()
    for ts in all_ts:
        iso_week = ts.isocalendar()
        week_counts[(iso_week[0], iso_week[1])] += 1
    today_dt = datetime.now(timezone.utc)
    weekly_labels = []
    weekly_values = []
    for i in range(11, -1, -1):
        d = today_dt - timedelta(weeks=i)
        iso = d.isocalendar()
        weekly_labels.append(f"W{iso[1]}")
        weekly_values.append(week_counts.get((iso[0], iso[1]), 0))

    # avg messages per session
    avg_msgs = round(sum(s["total_messages"] for s in sessions) / len(sessions), 1)
    avg_dur = round(sum(s["duration_min"] for s in sessions) / len(sessions), 1)
    total_user_msgs = sum(s["user_messages"] for s in sessions)

    # peak hour
    peak_hour = max(range(24), key=lambda h: hourly[h])

    # active days
    active_days = len(set(ts.date() for ts in all_ts))

    # message count distribution for scatter (bucketed)
    msg_dist = Counter()
    for s in sessions:
        bucket = min(s["total_messages"], 30)
        msg_dist[bucket] += 1
    msg_dist_labels = list(range(1, 31))
    msg_dist_values = [msg_dist.get(i, 0) for i in msg_dist_labels]

    # ── cost & ROI metrics ──
    total_cost = sum(s["estimated_cost"] for s in sessions)
    total_cost_without_cache = sum(s["cost_without_cache"] for s in sessions)
    cache_savings = total_cost_without_cache - total_cost

    t_input = sum(s["input_tokens"] for s in sessions)
    t_output = sum(s["output_tokens"] for s in sessions)
    t_cache_create = sum(s["cache_creation_tokens"] for s in sessions)
    t_cache_read = sum(s["cache_read_tokens"] for s in sessions)

    total_cache_tokens = t_cache_create + t_cache_read
    cache_hit_rate = round(t_cache_read / total_cache_tokens * 100, 1) if total_cache_tokens > 0 else 0.0
    avg_cost_per_session = round(total_cost / len(sessions), 4) if sessions else 0
    avg_tokens_per_session = round((t_input + t_output) / len(sessions)) if sessions else 0

    # daily cost
    daily_cost_map = defaultdict(float)
    for s in sessions:
        if s["first_ts"]:
            daily_cost_map[s["first_ts"].date()] += s["estimated_cost"]
    daily_cost_values = [round(daily_cost_map.get(datetime.fromisoformat(d).date(), 0), 2) for d in daily_labels]

    # weekly cost & cost efficiency
    weekly_cost_map = defaultdict(float)
    weekly_session_count = defaultdict(int)
    for s in sessions:
        if s["first_ts"]:
            iso = s["first_ts"].isocalendar()
            key = (iso[0], iso[1])
            weekly_cost_map[key] += s["estimated_cost"]
            weekly_session_count[key] += 1

    weekly_cost_values = []
    weekly_cost_per_session = []
    for i in range(11, -1, -1):
        d = today_dt - timedelta(weeks=i)
        iso = d.isocalendar()
        key = (iso[0], iso[1])
        cost = round(weekly_cost_map.get(key, 0), 2)
        weekly_cost_values.append(cost)
        count = weekly_session_count.get(key, 0)
        weekly_cost_per_session.append(round(cost / count, 2) if count > 0 else 0)

    # model cost breakdown
    model_cost_map = defaultdict(float)
    for s in sessions:
        for m, c in s["model_costs"].items():
            model_cost_map[m] += c
    model_cost_sorted = sorted(model_cost_map.items(), key=lambda x: x[1], reverse=True)
    model_cost_labels = [m for m, _ in model_cost_sorted[:8]]
    model_cost_values = [round(c, 2) for _, c in model_cost_sorted[:8]]

    # project cost
    proj_cost_map = defaultdict(float)
    for s in sessions:
        proj_cost_map[s["project"]] += s["estimated_cost"]
    top_projects_cost = sorted(proj_cost_map.items(), key=lambda x: x[1], reverse=True)[:10]

    # tool usage
    total_tool_counter = Counter()
    for s in sessions:
        for tool, count in s["tool_uses"].items():
            total_tool_counter[tool] += count
    tool_top = total_tool_counter.most_common(15)
    tool_labels = [t for t, _ in tool_top]
    tool_values = [c for _, c in tool_top]

    # depth cost
    depth_cost_map = defaultdict(list)
    for s in sessions:
        depth_cost_map[s["depth"]].append(s["estimated_cost"])
    depth_cost_values = [
        round(sum(depth_cost_map.get(d, [0])) / max(len(depth_cost_map.get(d, [1])), 1), 4)
        for d in depth_order
    ]

    # category cost
    cat_cost_map = defaultdict(float)
    for s in sessions:
        cat_cost_map[s["category"]] += s["estimated_cost"]
    cat_cost_sorted = sorted(cat_cost_map.items(), key=lambda x: x[1], reverse=True)
    cat_cost_labels = [c for c, _ in cat_cost_sorted]
    cat_cost_values = [round(v, 2) for _, v in cat_cost_sorted]

    return {
        "total_sessions": len(sessions),
        "total_user_messages": total_user_msgs,
        "active_days": active_days,
        "date_min": date_min.isoformat(),
        "date_max": date_max.isoformat(),
        "avg_msgs_per_session": avg_msgs,
        "avg_duration_min": avg_dur,
        "peak_hour": peak_hour,
        "hourly": hourly,
        "dow_labels": dow_labels,
        "dow_data": dow_data,
        "daily_labels": daily_labels,
        "daily_values": daily_values,
        "model_labels": [k for k, _ in model_counts.most_common(8)],
        "model_values": [v for _, v in model_counts.most_common(8)],
        "cat_labels": list(cat_counts.keys()),
        "cat_values": list(cat_counts.values()),
        "depth_labels": depth_order,
        "depth_values": depth_data,
        "top_projects": top_projects,
        "weekly_labels": weekly_labels,
        "weekly_values": weekly_values,
        "msg_dist_labels": msg_dist_labels,
        "msg_dist_values": msg_dist_values,
        # cost & ROI
        "total_cost": round(total_cost, 2),
        "total_cost_without_cache": round(total_cost_without_cache, 2),
        "cache_savings": round(cache_savings, 2),
        "cache_hit_rate": cache_hit_rate,
        "avg_cost_per_session": round(avg_cost_per_session, 2),
        "total_input_tokens": t_input,
        "total_output_tokens": t_output,
        "total_tokens_formatted": format_tokens(t_input + t_output),
        "avg_tokens_per_session": avg_tokens_per_session,
        "avg_tokens_formatted": format_tokens(avg_tokens_per_session),
        "daily_cost_values": daily_cost_values,
        "weekly_cost_values": weekly_cost_values,
        "weekly_cost_per_session": weekly_cost_per_session,
        "model_cost_labels": model_cost_labels,
        "model_cost_values": model_cost_values,
        "top_projects_cost": top_projects_cost,
        "tool_labels": tool_labels,
        "tool_values": tool_values,
        "depth_cost_values": depth_cost_values,
        "cat_cost_labels": cat_cost_labels,
        "cat_cost_values": cat_cost_values,
    }

# ─────────────────────────────────────────────
# HTML生成
# ─────────────────────────────────────────────

CHARTJS_CDN = "https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"

def j(v):
    return json.dumps(v, ensure_ascii=False)

def build_html(stats: dict, generated_at: str) -> str:
    s = stats
    peak_h = s["peak_hour"]
    peak_label = f"{peak_h:02d}:00〜{peak_h+1:02d}:00"

    # color palettes
    ACCENT   = "#00e5cc"
    ACCENT2  = "#f97316"
    ACCENT3  = "#a78bfa"
    ACCENT4  = "#34d399"
    ACCENT5  = "#ef4444"
    ACCENT6  = "#22d3ee"
    BG_CARD  = "rgba(255,255,255,0.04)"
    GRID     = "rgba(255,255,255,0.07)"
    TEXT_DIM = "rgba(255,255,255,0.45)"

    cat_colors = [
        "#00e5cc","#f97316","#a78bfa","#34d399","#fb923c","#60a5fa","#f472b6","#facc15"
    ]
    model_colors = [
        "#00e5cc","#f97316","#a78bfa","#34d399","#60a5fa","#f472b6","#facc15","#94a3b8"
    ]
    depth_colors = ["#1e3a5f","#1d4ed8","#3b82f6","#00e5cc"]

    top_proj_rows = "".join(
        f"""<tr>
          <td class="proj-name">{p}</td>
          <td class="proj-bar-cell">
            <div class="proj-bar-wrap">
              <div class="proj-bar" style="width:{round(c/s['top_projects'][0][1]*100)}%"></div>
            </div>
          </td>
          <td class="proj-count">{c}</td>
        </tr>"""
        for p, c in s["top_projects"]
    )

    max_proj_cost = s["top_projects_cost"][0][1] if s.get("top_projects_cost") else 1
    top_proj_cost_rows = "".join(
        f"""<tr>
          <td class="proj-name">{p}</td>
          <td class="proj-bar-cell">
            <div class="proj-bar-wrap">
              <div class="proj-bar" style="width:{round(c/max_proj_cost*100)}%;background:linear-gradient(90deg,{ACCENT5},rgba(239,68,68,0.5))"></div>
            </div>
          </td>
          <td class="proj-count" style="color:{ACCENT5}">${c:.2f}</td>
        </tr>"""
        for p, c in s.get("top_projects_cost", [])
    )

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Code Usage Report</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Noto+Sans+JP:wght@400;500;700&display=swap');

  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

  :root {{
    --accent:  {ACCENT};
    --accent2: {ACCENT2};
    --accent3: {ACCENT3};
    --bg:      #090c10;
    --bg2:     #0d1117;
    --card:    {BG_CARD};
    --border:  rgba(255,255,255,0.08);
    --text:    #e8edf3;
    --dim:     {TEXT_DIM};
    --grid:    {GRID};
    --mono:    'JetBrains Mono', monospace;
    --sans:    'Noto Sans JP', sans-serif;
  }}

  html {{ scroll-behavior: smooth; }}

  body {{
    background: var(--bg);
    color: var(--text);
    font-family: var(--sans);
    font-size: 14px;
    line-height: 1.6;
    min-height: 100vh;
  }}

  /* ── HEADER ── */
  .header {{
    position: relative;
    overflow: hidden;
    padding: 56px 48px 48px;
    border-bottom: 1px solid var(--border);
  }}
  .header::before {{
    content: '';
    position: absolute;
    inset: 0;
    background:
      radial-gradient(ellipse 60% 100% at 80% 50%, rgba(0,229,204,0.07) 0%, transparent 70%),
      radial-gradient(ellipse 40% 80% at 20% 20%, rgba(167,139,250,0.06) 0%, transparent 70%);
    pointer-events: none;
  }}
  .header-inner {{ max-width: 1200px; margin: 0 auto; position: relative; }}
  .header-badge {{
    display: inline-flex; align-items: center; gap: 6px;
    font-family: var(--mono); font-size: 11px; font-weight: 600;
    color: var(--accent); letter-spacing: 0.12em; text-transform: uppercase;
    border: 1px solid rgba(0,229,204,0.3); border-radius: 4px;
    padding: 3px 10px; margin-bottom: 20px;
  }}
  .header-badge::before {{
    content: ''; width: 6px; height: 6px; border-radius: 50%;
    background: var(--accent);
    box-shadow: 0 0 8px var(--accent);
    animation: pulse 2s infinite;
  }}
  @keyframes pulse {{
    0%, 100% {{ opacity: 1; }}
    50% {{ opacity: 0.4; }}
  }}
  .header-title {{
    font-family: var(--mono); font-size: clamp(28px, 4vw, 48px);
    font-weight: 700; letter-spacing: -0.02em;
    color: #fff;
    line-height: 1.1;
  }}
  .header-title span {{ color: var(--accent); }}
  .header-meta {{
    margin-top: 16px; color: var(--dim);
    font-family: var(--mono); font-size: 12px;
  }}

  /* ── LAYOUT ── */
  .main {{ max-width: 1200px; margin: 0 auto; padding: 40px 48px 80px; }}

  /* ── KPI ROW ── */
  .kpi-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 16px;
    margin-bottom: 40px;
  }}
  .kpi {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px 22px;
    position: relative;
    overflow: hidden;
    transition: border-color 0.2s;
  }}
  .kpi:hover {{ border-color: rgba(0,229,204,0.3); }}
  .kpi-label {{
    font-family: var(--mono); font-size: 10px; font-weight: 600;
    color: var(--dim); text-transform: uppercase; letter-spacing: 0.1em;
    margin-bottom: 8px;
  }}
  .kpi-value {{
    font-family: var(--mono); font-size: 32px; font-weight: 700;
    color: var(--accent); line-height: 1;
  }}
  .kpi-sub {{
    font-size: 11px; color: var(--dim); margin-top: 6px;
  }}
  .kpi-glow {{
    position: absolute; top: -20px; right: -20px;
    width: 80px; height: 80px; border-radius: 50%;
    background: radial-gradient(circle, rgba(0,229,204,0.12), transparent 70%);
  }}

  /* ── SECTION ── */
  .section-title {{
    font-family: var(--mono); font-size: 11px; font-weight: 600;
    color: var(--dim); text-transform: uppercase; letter-spacing: 0.12em;
    margin-bottom: 16px; display: flex; align-items: center; gap: 10px;
  }}
  .section-title::after {{
    content: ''; flex: 1; height: 1px; background: var(--border);
  }}

  /* ── GRID LAYOUTS ── */
  .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 20px; }}
  .grid-3 {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 20px; margin-bottom: 20px; }}
  .col-span-2 {{ grid-column: span 2; }}
  .col-span-3 {{ grid-column: span 3; }}

  @media (max-width: 900px) {{
    .main {{ padding: 24px 20px 60px; }}
    .header {{ padding: 36px 20px 32px; }}
    .grid-2, .grid-3 {{ grid-template-columns: 1fr; }}
    .col-span-2, .col-span-3 {{ grid-column: span 1; }}
  }}

  /* ── CARD ── */
  .card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 24px;
    transition: border-color 0.2s;
  }}
  .card:hover {{ border-color: rgba(255,255,255,0.15); }}
  .card-title {{
    font-family: var(--mono); font-size: 13px; font-weight: 600;
    color: var(--text); margin-bottom: 20px;
    display: flex; align-items: center; gap: 8px;
  }}
  .card-title .dot {{
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--accent);
    box-shadow: 0 0 6px var(--accent);
  }}

  /* ── CHART WRAPPER ── */
  .chart-wrap {{
    position: relative;
    width: 100%; 
  }}
  .chart-wrap canvas {{
    max-width: 100%;
  }}

  /* ── HEATMAP ── */
  .heatmap {{
    display: grid;
    grid-template-columns: repeat(24, 1fr);
    gap: 3px;
    margin-top: 8px;
  }}
  .heatmap-cell {{
    aspect-ratio: 1;
    border-radius: 4px;
    position: relative;
    cursor: default;
    transition: transform 0.15s;
  }}
  .heatmap-cell:hover {{ transform: scale(1.2); z-index: 10; }}
  .heatmap-labels {{
    display: grid;
    grid-template-columns: repeat(24, 1fr);
    gap: 3px;
    margin-top: 4px;
  }}
  .heatmap-labels span {{
    font-family: var(--mono); font-size: 9px;
    color: var(--dim); text-align: center;
  }}

  /* ── TOP PROJECTS TABLE ── */
  .proj-table {{ width: 100%; border-collapse: collapse; }}
  .proj-table tr {{ border-bottom: 1px solid var(--border); }}
  .proj-table tr:last-child {{ border-bottom: none; }}
  .proj-table td {{ padding: 10px 0; vertical-align: middle; }}
  .proj-name {{
    font-family: var(--mono); font-size: 11px; color: var(--text);
    max-width: 240px; white-space: nowrap; overflow: hidden;
    text-overflow: ellipsis; padding-right: 16px !important;
  }}
  .proj-bar-cell {{ width: 100%; }}
  .proj-bar-wrap {{
    background: rgba(255,255,255,0.05); border-radius: 4px; height: 8px; overflow: hidden;
  }}
  .proj-bar {{
    height: 100%; border-radius: 4px;
    background: linear-gradient(90deg, var(--accent), rgba(0,229,204,0.5));
    transition: width 0.8s cubic-bezier(0.4,0,0.2,1);
  }}
  .proj-count {{
    font-family: var(--mono); font-size: 12px; font-weight: 600;
    color: var(--accent); padding-left: 12px !important;
    white-space: nowrap;
  }}

  /* ── FOOTER ── */
  .footer {{
    text-align: center; padding: 32px;
    font-family: var(--mono); font-size: 11px; color: var(--dim);
    border-top: 1px solid var(--border);
  }}
  .mb-20 {{ margin-bottom: 20px; }}
  .mb-40 {{ margin-bottom: 40px; }}
</style>
</head>
<body>

<header class="header">
  <div class="header-inner">
    <div class="header-badge">● Claude Code Analytics</div>
    <h1 class="header-title">Usage<br><span>Report</span></h1>
    <p class="header-meta">
      生成日時: {generated_at} &nbsp;|&nbsp;
      期間: {s['date_min']} 〜 {s['date_max']} &nbsp;|&nbsp;
      完全ローカル処理 / ネットワーク通信なし
    </p>
  </div>
</header>

<main class="main">

  <!-- KPI -->
  <div class="kpi-grid mb-40">
    <div class="kpi">
      <div class="kpi-glow"></div>
      <div class="kpi-label">Total Sessions</div>
      <div class="kpi-value">{s['total_sessions']}</div>
      <div class="kpi-sub">会話セッション数</div>
    </div>
    <div class="kpi">
      <div class="kpi-glow" style="background:radial-gradient(circle,rgba(249,115,22,0.12),transparent 70%)"></div>
      <div class="kpi-label">User Messages</div>
      <div class="kpi-value" style="color:var(--accent2)">{s['total_user_messages']}</div>
      <div class="kpi-sub">送信メッセージ総数</div>
    </div>
    <div class="kpi">
      <div class="kpi-glow" style="background:radial-gradient(circle,rgba(167,139,250,0.12),transparent 70%)"></div>
      <div class="kpi-label">Active Days</div>
      <div class="kpi-value" style="color:var(--accent3)">{s['active_days']}</div>
      <div class="kpi-sub">利用日数</div>
    </div>
    <div class="kpi">
      <div class="kpi-glow" style="background:radial-gradient(circle,rgba(52,211,153,0.12),transparent 70%)"></div>
      <div class="kpi-label">Avg Msgs/Session</div>
      <div class="kpi-value" style="color:#34d399">{s['avg_msgs_per_session']}</div>
      <div class="kpi-sub">セッション平均メッセージ数</div>
    </div>
    <div class="kpi">
      <div class="kpi-glow"></div>
      <div class="kpi-label">Avg Duration</div>
      <div class="kpi-value">{s['avg_duration_min']}<span style="font-size:16px;font-weight:400">min</span></div>
      <div class="kpi-sub">平均セッション時間</div>
    </div>
    <div class="kpi">
      <div class="kpi-glow" style="background:radial-gradient(circle,rgba(249,115,22,0.12),transparent 70%)"></div>
      <div class="kpi-label">Peak Hour</div>
      <div class="kpi-value" style="color:var(--accent2)">{peak_label}</div>
      <div class="kpi-sub">最も活発な時間帯</div>
    </div>
  </div>

  <!-- コスト分析 KPI -->
  <div class="section-title">コスト分析 (ROI)</div>
  <div class="kpi-grid mb-40">
    <div class="kpi">
      <div class="kpi-glow" style="background:radial-gradient(circle,rgba(239,68,68,0.12),transparent 70%)"></div>
      <div class="kpi-label">Total Cost (USD)</div>
      <div class="kpi-value" style="color:{ACCENT5}">${s['total_cost']}</div>
      <div class="kpi-sub">推定総コスト</div>
    </div>
    <div class="kpi">
      <div class="kpi-glow" style="background:radial-gradient(circle,rgba(239,68,68,0.12),transparent 70%)"></div>
      <div class="kpi-label">Avg Cost/Session</div>
      <div class="kpi-value" style="color:{ACCENT5}">${s['avg_cost_per_session']}</div>
      <div class="kpi-sub">セッション平均コスト</div>
    </div>
    <div class="kpi">
      <div class="kpi-glow" style="background:radial-gradient(circle,rgba(34,211,238,0.12),transparent 70%)"></div>
      <div class="kpi-label">Cache Savings</div>
      <div class="kpi-value" style="color:{ACCENT6}">${s['cache_savings']}</div>
      <div class="kpi-sub">キャッシュによる推定削減額</div>
    </div>
    <div class="kpi">
      <div class="kpi-glow" style="background:radial-gradient(circle,rgba(34,211,238,0.12),transparent 70%)"></div>
      <div class="kpi-label">Cache Hit Rate</div>
      <div class="kpi-value" style="color:{ACCENT6}">{s['cache_hit_rate']}%</div>
      <div class="kpi-sub">キャッシュヒット率</div>
    </div>
    <div class="kpi">
      <div class="kpi-glow"></div>
      <div class="kpi-label">Total Tokens</div>
      <div class="kpi-value">{s['total_tokens_formatted']}</div>
      <div class="kpi-sub">入力+出力トークン総数</div>
    </div>
    <div class="kpi">
      <div class="kpi-glow"></div>
      <div class="kpi-label">Avg Tokens/Session</div>
      <div class="kpi-value">{s['avg_tokens_formatted']}</div>
      <div class="kpi-sub">セッション平均トークン数</div>
    </div>
  </div>

  <!-- 日別アクティビティ -->
  <div class="section-title">時系列アクティビティ</div>
  <div class="card mb-20">
    <div class="card-title"><span class="dot"></span>直近90日間のセッション数</div>
    <div class="chart-wrap" style="height:120px">
      <canvas id="dailyChart"></canvas>
    </div>
  </div>

  <!-- 日別コスト推移 -->
  <div class="card mb-20">
    <div class="card-title"><span class="dot" style="background:{ACCENT5};box-shadow:0 0 6px {ACCENT5}"></span>日別コスト推移 (USD)</div>
    <div class="chart-wrap" style="height:120px">
      <canvas id="dailyCostChart"></canvas>
    </div>
  </div>

  <!-- 週次トレンド + 時間帯ヒートマップ -->
  <div class="grid-2 mb-20">
    <div class="card">
      <div class="card-title"><span class="dot" style="background:var(--accent2);box-shadow:0 0 6px var(--accent2)"></span>週次トレンド（直近12週）</div>
      <div class="chart-wrap" style="height:180px">
        <canvas id="weeklyChart"></canvas>
      </div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot" style="background:var(--accent3);box-shadow:0 0 6px var(--accent3)"></span>曜日別分布</div>
      <div class="chart-wrap" style="height:180px">
        <canvas id="dowChart"></canvas>
      </div>
    </div>
  </div>

  <!-- 週次コスト推移 + コスト効率 -->
  <div class="grid-2 mb-20">
    <div class="card">
      <div class="card-title"><span class="dot" style="background:{ACCENT5};box-shadow:0 0 6px {ACCENT5}"></span>週次コスト推移 (USD)</div>
      <div class="chart-wrap" style="height:180px">
        <canvas id="weeklyCostChart"></canvas>
      </div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot" style="background:{ACCENT6};box-shadow:0 0 6px {ACCENT6}"></span>コスト効率トレンド (USD/session)</div>
      <div class="chart-wrap" style="height:180px">
        <canvas id="costEffChart"></canvas>
      </div>
    </div>
  </div>

  <!-- 時間帯ヒートマップ -->
  <div class="card mb-40">
    <div class="card-title"><span class="dot"></span>時間帯ヒートマップ（0〜23時）</div>
    <div class="heatmap" id="heatmapGrid"></div>
    <div class="heatmap-labels" id="heatmapLabels"></div>
  </div>

  <!-- カテゴリ + モデル -->
  <div class="section-title">会話内容・モデル分析</div>
  <div class="grid-2 mb-20">
    <div class="card">
      <div class="card-title"><span class="dot" style="background:var(--accent2);box-shadow:0 0 6px var(--accent2)"></span>会話カテゴリ分類</div>
      <div class="chart-wrap" style="height:260px">
        <canvas id="catChart"></canvas>
      </div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot" style="background:var(--accent3);box-shadow:0 0 6px var(--accent3)"></span>使用モデル分布</div>
      <div class="chart-wrap" style="height:260px">
        <canvas id="modelChart"></canvas>
      </div>
    </div>
  </div>

  <!-- コスト内訳分析 -->
  <div class="section-title">コスト内訳分析</div>
  <div class="grid-2 mb-20">
    <div class="card">
      <div class="card-title"><span class="dot" style="background:{ACCENT5};box-shadow:0 0 6px {ACCENT5}"></span>モデル別コスト内訳 (USD)</div>
      <div class="chart-wrap" style="height:260px">
        <canvas id="modelCostChart"></canvas>
      </div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot" style="background:var(--accent2);box-shadow:0 0 6px var(--accent2)"></span>カテゴリ別投資バランス (USD)</div>
      <div class="chart-wrap" style="height:260px">
        <canvas id="catCostChart"></canvas>
      </div>
    </div>
  </div>
  <div class="grid-2 mb-40">
    <div class="card">
      <div class="card-title"><span class="dot" style="background:{ACCENT5};box-shadow:0 0 6px {ACCENT5}"></span>セッション深度別 平均コスト (USD)</div>
      <div class="chart-wrap" style="height:200px">
        <canvas id="depthCostChart"></canvas>
      </div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot" style="background:{ACCENT6};box-shadow:0 0 6px {ACCENT6}"></span>ツール使用ランキング</div>
      <div class="chart-wrap" style="height:200px">
        <canvas id="toolChart"></canvas>
      </div>
    </div>
  </div>

  <!-- セッション深度 + メッセージ数分布 -->
  <div class="section-title">セッション深度</div>
  <div class="grid-2 mb-40">
    <div class="card">
      <div class="card-title"><span class="dot"></span>セッション深度分類</div>
      <div class="chart-wrap" style="height:200px">
        <canvas id="depthChart"></canvas>
      </div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot" style="background:var(--accent2);box-shadow:0 0 6px var(--accent2)"></span>メッセージ数分布</div>
      <div class="chart-wrap" style="height:200px">
        <canvas id="msgDistChart"></canvas>
      </div>
    </div>
  </div>

  <!-- Top Projects -->
  <div class="section-title">プロジェクト分析</div>
  <div class="card mb-20">
    <div class="card-title"><span class="dot"></span>セッション数トップ10プロジェクト</div>
    <table class="proj-table">
      {top_proj_rows}
    </table>
  </div>
  <div class="card mb-40">
    <div class="card-title"><span class="dot" style="background:{ACCENT5};box-shadow:0 0 6px {ACCENT5}"></span>コストランキング TOP10 (USD)</div>
    <table class="proj-table">
      {top_proj_cost_rows}
    </table>
  </div>

</main>

<footer class="footer">
  Claude Code Usage Report &nbsp;·&nbsp; Generated locally &nbsp;·&nbsp; No data sent externally
</footer>

<script src="{CHARTJS_CDN}"></script>
<script>
// ─── Data ───
const hourly     = {j(s['hourly'])};
const dowLabels  = {j(s['dow_labels'])};
const dowData    = {j(s['dow_data'])};
const dailyLbls  = {j(s['daily_labels'])};
const dailyVals  = {j(s['daily_values'])};
const weeklyLbls = {j(s['weekly_labels'])};
const weeklyVals = {j(s['weekly_values'])};
const catLabels  = {j(s['cat_labels'])};
const catValues  = {j(s['cat_values'])};
const modelLbls  = {j(s['model_labels'])};
const modelVals  = {j(s['model_values'])};
const depthLbls  = {j(s['depth_labels'])};
const depthVals  = {j(s['depth_values'])};
const msgDistL   = {j(s['msg_dist_labels'])};
const msgDistV   = {j(s['msg_dist_values'])};

const dailyCostVals = {j(s['daily_cost_values'])};
const weeklyCostVals = {j(s['weekly_cost_values'])};
const costPerSession = {j(s['weekly_cost_per_session'])};
const modelCostLbls = {j(s['model_cost_labels'])};
const modelCostVals = {j(s['model_cost_values'])};
const catCostLbls = {j(s['cat_cost_labels'])};
const catCostVals = {j(s['cat_cost_values'])};
const depthCostVals = {j(s['depth_cost_values'])};
const toolLbls = {j(s['tool_labels'])};
const toolVals = {j(s['tool_values'])};

const ACCENT   = '{ACCENT}';
const ACCENT2  = '{ACCENT2}';
const ACCENT3  = '{ACCENT3}';
const ACCENT4  = '{ACCENT4}';
const ACCENT5  = '{ACCENT5}';
const ACCENT6  = '{ACCENT6}';
const GRID     = '{GRID}';
const TEXT_DIM = '{TEXT_DIM}';
const TEXT     = '#e8edf3';

const catColors   = {j(cat_colors)};
const modelColors = {j(model_colors)};
const depthColors = {j(depth_colors)};

Chart.defaults.color = TEXT_DIM;
Chart.defaults.font.family = "'JetBrains Mono', monospace";
Chart.defaults.font.size = 11;

const gridOpts = {{
  color: GRID,
  drawBorder: false,
}};
const noLegend  = {{ display: false }};
const defTooltip = {{
  backgroundColor: 'rgba(10,14,20,0.95)',
  borderColor: 'rgba(255,255,255,0.1)',
  borderWidth: 1,
  titleColor: TEXT,
  bodyColor: TEXT_DIM,
  padding: 10,
}};

// ─── Daily activity ───
new Chart(document.getElementById('dailyChart'), {{
  type: 'bar',
  data: {{
    labels: dailyLbls,
    datasets: [{{
      data: dailyVals,
      backgroundColor: ctx => {{
        const g = ctx.chart.ctx.createLinearGradient(0, 0, 0, 120);
        g.addColorStop(0, 'rgba(0,229,204,0.8)');
        g.addColorStop(1, 'rgba(0,229,204,0.1)');
        return g;
      }},
      borderRadius: 3,
      borderSkipped: false,
    }}]
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    plugins: {{ legend: noLegend, tooltip: {{ ...defTooltip, callbacks: {{
      title: items => items[0].label,
      label: item => ` ${{item.raw}} セッション`,
    }} }} }},
    scales: {{
      x: {{ display: false }},
      y: {{ grid: gridOpts, ticks: {{ stepSize: 1 }} }},
    }},
  }}
}});

// ─── Weekly ───
new Chart(document.getElementById('weeklyChart'), {{
  type: 'line',
  data: {{
    labels: weeklyLbls,
    datasets: [{{
      data: weeklyVals,
      borderColor: ACCENT2,
      backgroundColor: 'rgba(249,115,22,0.1)',
      borderWidth: 2,
      pointBackgroundColor: ACCENT2,
      pointRadius: 4,
      tension: 0.3,
      fill: true,
    }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: noLegend, tooltip: defTooltip }},
    scales: {{
      x: {{ grid: gridOpts }},
      y: {{ grid: gridOpts, beginAtZero: true, ticks: {{ stepSize: 1 }} }},
    }},
  }}
}});

// ─── Day of week ───
new Chart(document.getElementById('dowChart'), {{
  type: 'bar',
  data: {{
    labels: dowLabels,
    datasets: [{{
      data: dowData,
      backgroundColor: dowData.map((_, i) =>
        i >= 5 ? 'rgba(167,139,250,0.7)' : 'rgba(167,139,250,0.35)'
      ),
      borderRadius: 6,
      borderSkipped: false,
    }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: noLegend, tooltip: defTooltip }},
    scales: {{
      x: {{ grid: {{ display: false }} }},
      y: {{ grid: gridOpts, beginAtZero: true, ticks: {{ stepSize: 1 }} }},
    }},
  }}
}});

// ─── Heatmap ───
(function() {{
  const maxVal = Math.max(...hourly, 1);
  const grid  = document.getElementById('heatmapGrid');
  const lbls  = document.getElementById('heatmapLabels');
  hourly.forEach((v, h) => {{
    const cell = document.createElement('div');
    cell.className = 'heatmap-cell';
    const intensity = v / maxVal;
    const alpha = 0.08 + intensity * 0.85;
    cell.style.background = `rgba(0,229,204,${{alpha.toFixed(2)}})`;
    cell.title = `${{h}}:00 — ${{v}} セッション`;
    grid.appendChild(cell);
    const lbl = document.createElement('span');
    lbl.textContent = h % 3 === 0 ? h : '';
    lbls.appendChild(lbl);
  }});
}})();

// ─── Category (doughnut) ───
new Chart(document.getElementById('catChart'), {{
  type: 'doughnut',
  data: {{
    labels: catLabels,
    datasets: [{{ data: catValues, backgroundColor: catColors, borderWidth: 0, hoverOffset: 6 }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    cutout: '65%',
    plugins: {{
      legend: {{ position: 'right', labels: {{ boxWidth: 12, padding: 12, color: TEXT_DIM }} }},
      tooltip: defTooltip,
    }},
  }}
}});

// ─── Model (doughnut) ───
new Chart(document.getElementById('modelChart'), {{
  type: 'doughnut',
  data: {{
    labels: modelLbls,
    datasets: [{{ data: modelVals, backgroundColor: modelColors, borderWidth: 0, hoverOffset: 6 }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    cutout: '65%',
    plugins: {{
      legend: {{ position: 'right', labels: {{ boxWidth: 12, padding: 12, color: TEXT_DIM }} }},
      tooltip: defTooltip,
    }},
  }}
}});

// ─── Session depth ───
new Chart(document.getElementById('depthChart'), {{
  type: 'bar',
  data: {{
    labels: depthLbls,
    datasets: [{{
      data: depthVals,
      backgroundColor: depthColors,
      borderRadius: 8,
      borderSkipped: false,
    }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    indexAxis: 'y',
    plugins: {{ legend: noLegend, tooltip: defTooltip }},
    scales: {{
      x: {{ grid: gridOpts, beginAtZero: true }},
      y: {{ grid: {{ display: false }} }},
    }},
  }}
}});

// ─── Msg distribution ───
new Chart(document.getElementById('msgDistChart'), {{
  type: 'bar',
  data: {{
    labels: msgDistL.map(v => v === 30 ? '30+' : String(v)),
    datasets: [{{
      data: msgDistV,
      backgroundColor: ctx => {{
        const g = ctx.chart.ctx.createLinearGradient(0, 0, 0, 200);
        g.addColorStop(0, 'rgba(249,115,22,0.8)');
        g.addColorStop(1, 'rgba(249,115,22,0.1)');
        return g;
      }},
      borderRadius: 3,
      borderSkipped: false,
    }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: noLegend, tooltip: {{ ...defTooltip, callbacks: {{
      title: items => `${{items[0].label}} メッセージ`,
      label: item => ` ${{item.raw}} セッション`,
    }} }} }},
    scales: {{
      x: {{ grid: {{ display: false }}, ticks: {{ maxTicksLimit: 10 }} }},
      y: {{ grid: gridOpts, beginAtZero: true }},
    }},
  }}
}});

// ─── Daily cost ───
new Chart(document.getElementById('dailyCostChart'), {{
  type: 'bar',
  data: {{
    labels: dailyLbls,
    datasets: [{{
      data: dailyCostVals,
      backgroundColor: ctx => {{
        const g = ctx.chart.ctx.createLinearGradient(0, 0, 0, 120);
        g.addColorStop(0, 'rgba(239,68,68,0.8)');
        g.addColorStop(1, 'rgba(239,68,68,0.1)');
        return g;
      }},
      borderRadius: 3,
      borderSkipped: false,
    }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: noLegend, tooltip: {{ ...defTooltip, callbacks: {{
      title: items => items[0].label,
      label: item => ` $${{item.raw.toFixed(2)}}`,
    }} }} }},
    scales: {{
      x: {{ display: false }},
      y: {{ grid: gridOpts, ticks: {{ callback: v => '$' + v }} }},
    }},
  }}
}});

// ─── Weekly cost ───
new Chart(document.getElementById('weeklyCostChart'), {{
  type: 'bar',
  data: {{
    labels: weeklyLbls,
    datasets: [{{
      data: weeklyCostVals,
      backgroundColor: 'rgba(239,68,68,0.6)',
      borderRadius: 6,
      borderSkipped: false,
    }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: noLegend, tooltip: {{ ...defTooltip, callbacks: {{
      label: item => ` $${{item.raw.toFixed(2)}}`,
    }} }} }},
    scales: {{
      x: {{ grid: {{ display: false }} }},
      y: {{ grid: gridOpts, beginAtZero: true, ticks: {{ callback: v => '$' + v }} }},
    }},
  }}
}});

// ─── Cost efficiency trend ───
new Chart(document.getElementById('costEffChart'), {{
  type: 'line',
  data: {{
    labels: weeklyLbls,
    datasets: [{{
      data: costPerSession,
      borderColor: ACCENT6,
      backgroundColor: 'rgba(34,211,238,0.1)',
      borderWidth: 2,
      pointBackgroundColor: ACCENT6,
      pointRadius: 4,
      tension: 0.3,
      fill: true,
    }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: noLegend, tooltip: {{ ...defTooltip, callbacks: {{
      label: item => ` $${{item.raw.toFixed(2)}} / session`,
    }} }} }},
    scales: {{
      x: {{ grid: gridOpts }},
      y: {{ grid: gridOpts, beginAtZero: true, ticks: {{ callback: v => '$' + v }} }},
    }},
  }}
}});

// ─── Model cost (doughnut) ───
new Chart(document.getElementById('modelCostChart'), {{
  type: 'doughnut',
  data: {{
    labels: modelCostLbls,
    datasets: [{{ data: modelCostVals, backgroundColor: ['#ef4444','#f97316','#facc15','#34d399','#60a5fa','#a78bfa','#f472b6','#94a3b8'], borderWidth: 0, hoverOffset: 6 }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    cutout: '65%',
    plugins: {{
      legend: {{ position: 'right', labels: {{ boxWidth: 12, padding: 12, color: TEXT_DIM }} }},
      tooltip: {{ ...defTooltip, callbacks: {{
        label: item => ` $${{item.raw.toFixed(2)}}`,
      }} }},
    }},
  }}
}});

// ─── Category cost (doughnut) ───
new Chart(document.getElementById('catCostChart'), {{
  type: 'doughnut',
  data: {{
    labels: catCostLbls,
    datasets: [{{ data: catCostVals, backgroundColor: catColors, borderWidth: 0, hoverOffset: 6 }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    cutout: '65%',
    plugins: {{
      legend: {{ position: 'right', labels: {{ boxWidth: 12, padding: 12, color: TEXT_DIM }} }},
      tooltip: {{ ...defTooltip, callbacks: {{
        label: item => ` $${{item.raw.toFixed(2)}}`,
      }} }},
    }},
  }}
}});

// ─── Depth cost (horizontal bar) ───
new Chart(document.getElementById('depthCostChart'), {{
  type: 'bar',
  data: {{
    labels: depthLbls,
    datasets: [{{
      data: depthCostVals,
      backgroundColor: ['#7f1d1d','#dc2626','#f87171','#fca5a5'],
      borderRadius: 8,
      borderSkipped: false,
    }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    indexAxis: 'y',
    plugins: {{ legend: noLegend, tooltip: {{ ...defTooltip, callbacks: {{
      label: item => ` $${{item.raw.toFixed(4)}} / session`,
    }} }} }},
    scales: {{
      x: {{ grid: gridOpts, beginAtZero: true, ticks: {{ callback: v => '$' + v }} }},
      y: {{ grid: {{ display: false }} }},
    }},
  }}
}});

// ─── Tool usage (horizontal bar) ───
new Chart(document.getElementById('toolChart'), {{
  type: 'bar',
  data: {{
    labels: toolLbls,
    datasets: [{{
      data: toolVals,
      backgroundColor: 'rgba(34,211,238,0.6)',
      borderRadius: 4,
      borderSkipped: false,
    }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    indexAxis: 'y',
    plugins: {{ legend: noLegend, tooltip: defTooltip }},
    scales: {{
      x: {{ grid: gridOpts, beginAtZero: true }},
      y: {{ grid: {{ display: false }}, ticks: {{ font: {{ size: 10 }} }} }},
    }},
  }}
}});

</script>
</body>
</html>"""
    return html


# ─────────────────────────────────────────────
# エントリポイント
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Claude Code 使用状況レポートをローカル生成します"
    )
    parser.add_argument(
        "--claude-dir",
        default=str(Path.home() / ".claude"),
        help="Claude設定ディレクトリ（デフォルト: ~/.claude）"
    )
    parser.add_argument(
        "--days", type=int, default=0,
        help="集計対象日数（0=全期間, 例: 90=直近90日）"
    )
    parser.add_argument(
        "--output",
        default="report.html",
        help="出力ファイルパス"
    )
    parser.add_argument(
        "--extract-only", action="store_true",
        help="データ抽出のみ（JSON出力）"
    )
    args = parser.parse_args()

    claude_dir = Path(args.claude_dir)
    if not claude_dir.exists():
        print(f"❌ Claude ディレクトリが見つかりません: {claude_dir}", file=sys.stderr)
        print("   Claude Code をインストール・起動して、会話履歴を作成してください。", file=sys.stderr)
        sys.exit(1)

    print(f"📂 読み取り中: {claude_dir}/projects/ ...", file=sys.stderr)
    sessions = parse_projects(claude_dir, args.days)

    if not sessions:
        print("⚠️  会話データが見つかりませんでした。", file=sys.stderr)
        sys.exit(1)

    print(f"✅ {len(sessions)} セッションを検出", file=sys.stderr)
    stats = compute_stats(sessions)

    if args.extract_only:
        print(json.dumps(stats, ensure_ascii=False, default=str, indent=2))
        return

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html = build_html(stats, generated_at)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")

    print(f"🎉 レポート生成完了: {output_path}", file=sys.stderr)
    print(f"   open {output_path}", file=sys.stderr)

if __name__ == "__main__":
    main()

