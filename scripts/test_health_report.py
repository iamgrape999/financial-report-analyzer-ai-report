#!/usr/bin/env python3
"""
Test Health Report Generator
=============================
Reads .claude/test_history.jsonl and emits a prioritised Markdown report to
.claude/test_health.md  (and optionally stdout with --print).

Categories
----------
🔴 Broken   — failed in the last ≥3 consecutive runs
🟡 Flaky    — mixed pass/fail in the last 20 runs (10–90% failure rate)
🐌 Slow     — mean duration of last-10 > 2× first-10 (trending) OR mean > 8s
✅ (omitted in report — only weak tests are listed)

Evidence line format
--------------------
  Broken: "Failed 5/5 recent runs — last failure 2026-05-17 10:23"
  Flaky:  "Failed 4/18 runs (22%) — failures on 05-16 14:11, 05-15 09:42"
  Slow:   "Avg 12.3s (last 10 runs), was 3.1s baseline (+297%) — worst 18.4s"

Usage
-----
  python3 scripts/test_health_report.py            # write report, silent
  python3 scripts/test_health_report.py --print    # write + print to stdout
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).parent.parent
_HISTORY_FILE = _ROOT / ".claude" / "test_history.jsonl"
_REPORT_FILE = _ROOT / ".claude" / "test_health.md"

# ── Thresholds ────────────────────────────────────────────────────────────────
_WINDOW_RECENT = 20       # runs considered "recent" for flaky / broken checks
_BROKEN_MIN_CONSECUTIVE = 3  # consecutive failures to call a test broken
_FLAKY_MIN_FAILURE_RATE = 0.10   # 10% failure rate minimum
_FLAKY_MAX_FAILURE_RATE = 0.90   # above 90% → broken, not flaky
_SLOW_ABSOLUTE_S = 8.0    # mean > 8s → slow regardless of trend
_SLOW_TREND_FACTOR = 2.0  # mean(last-10) > 2× mean(first-10) → trending slow
_SLOW_TREND_MIN_RUNS = 20  # need at least this many runs to declare a trend
_SLOW_BASELINE_WINDOW = 10
_SLOW_RECENT_WINDOW = 10
_MAX_HISTORY_PER_TEST = 100  # oldest records trimmed after this


def _load_history() -> dict[str, list[dict]]:
    """Return {test_id: [records...]} newest-last."""
    if not _HISTORY_FILE.exists():
        return {}
    buckets: dict[str, list[dict]] = defaultdict(list)
    with _HISTORY_FILE.open() as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
                if "test_id" in rec and "status" in rec:
                    buckets[rec["test_id"]].append(rec)
            except json.JSONDecodeError:
                pass
    # Sort by timestamp (ascending), cap history
    out: dict[str, list[dict]] = {}
    for tid, recs in buckets.items():
        recs.sort(key=lambda r: r.get("ts", 0))
        out[tid] = recs[-_MAX_HISTORY_PER_TEST:]
    return out


def _ts_label(ts: int) -> str:
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%M")
    except Exception:
        return "?"


def _analyse(history: dict[str, list[dict]]) -> tuple[list[dict], list[dict], list[dict]]:
    """Return (broken, flaky, slow) — each item is an analysis dict."""
    broken: list[dict] = []
    flaky: list[dict] = []
    slow: list[dict] = []

    for tid, recs in history.items():
        # Only examine real test calls (skip skipped / xfailed / rerun-intermediate)
        calls = [r for r in recs if r["status"] in ("passed", "failed", "error")]
        if len(calls) < 2:
            continue

        recent = calls[-_WINDOW_RECENT:]
        durations = [r.get("duration", 0.0) for r in calls if r.get("duration") is not None]

        # ── Broken check ─────────────────────────────────────────────────────
        tail = calls[-_BROKEN_MIN_CONSECUTIVE:]
        if all(r["status"] in ("failed", "error") for r in tail) and len(tail) >= _BROKEN_MIN_CONSECUTIVE:
            consec = 0
            for r in reversed(calls):
                if r["status"] in ("failed", "error"):
                    consec += 1
                else:
                    break
            last_rec = calls[-1]
            broken.append({
                "test_id": tid,
                "consecutive": consec,
                "total_checked": len(calls),
                "last_ts": _ts_label(last_rec.get("ts", 0)),
                "last_duration": last_rec.get("duration", 0.0),
            })
            continue  # broken takes priority over flaky

        # ── Flaky check ──────────────────────────────────────────────────────
        fail_count = sum(1 for r in recent if r["status"] in ("failed", "error"))
        pass_count = sum(1 for r in recent if r["status"] == "passed")
        if fail_count >= 1 and pass_count >= 1:
            rate = fail_count / len(recent)
            if _FLAKY_MIN_FAILURE_RATE <= rate <= _FLAKY_MAX_FAILURE_RATE:
                failure_dates = [
                    _ts_label(r.get("ts", 0))
                    for r in recent
                    if r["status"] in ("failed", "error")
                ][-4:]  # last 4 failure timestamps
                flaky.append({
                    "test_id": tid,
                    "fail_count": fail_count,
                    "window": len(recent),
                    "rate_pct": round(rate * 100, 1),
                    "failure_dates": failure_dates,
                    "last_ts": _ts_label(recent[-1].get("ts", 0)),
                })

        # ── Slow check ───────────────────────────────────────────────────────
        if len(durations) < 2:
            continue
        mean_recent = sum(durations[-_SLOW_RECENT_WINDOW:]) / min(len(durations), _SLOW_RECENT_WINDOW)
        worst = max(durations[-_SLOW_RECENT_WINDOW:])

        is_slow_abs = mean_recent >= _SLOW_ABSOLUTE_S
        is_trending = False
        trend_factor = None
        if len(durations) >= _SLOW_TREND_MIN_RUNS:
            mean_base = sum(durations[:_SLOW_BASELINE_WINDOW]) / _SLOW_BASELINE_WINDOW
            if mean_base > 0.01:
                trend_factor = mean_recent / mean_base
                is_trending = trend_factor >= _SLOW_TREND_FACTOR

        if is_slow_abs or is_trending:
            slow.append({
                "test_id": tid,
                "mean_recent_s": round(mean_recent, 2),
                "worst_s": round(worst, 2),
                "trend_factor": round(trend_factor, 1) if trend_factor else None,
                "is_trending": is_trending,
                "is_slow_abs": is_slow_abs,
                "run_count": len(durations),
            })

    # Sort by severity
    broken.sort(key=lambda x: -x["consecutive"])
    flaky.sort(key=lambda x: (-x["fail_count"], -x["rate_pct"]))
    slow.sort(key=lambda x: -x["mean_recent_s"])

    return broken, flaky, slow


def _short_name(test_id: str) -> str:
    """Trim long pytest node IDs to fit a single line."""
    parts = test_id.split("::")
    if len(parts) >= 3:
        return f"{Path(parts[0]).name}::{parts[1]}::{parts[-1]}"
    return test_id


def _render_report(
    history: dict[str, list[dict]],
    broken: list[dict],
    flaky: list[dict],
    slow: list[dict],
) -> str:
    total_tests = len(history)
    total_runs = sum(len(v) for v in history.values())
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines: list[str] = [
        "# 🧪 Test Health Report",
        f"> Generated {now} · {total_tests} tests · {total_runs} recorded runs",
        "",
    ]

    if not broken and not flaky and not slow:
        lines += [
            "✅ **All tests are healthy** — no broken, flaky, or slow tests detected.",
            "",
            f"_(Needs at least {_BROKEN_MIN_CONSECUTIVE} runs per test to detect issues)_",
        ]
        return "\n".join(lines)

    # Summary banner
    summary_parts = []
    if broken:
        summary_parts.append(f"🔴 {len(broken)} broken")
    if flaky:
        summary_parts.append(f"🟡 {len(flaky)} flaky")
    if slow:
        summary_parts.append(f"🐌 {len(slow)} slow")
    lines += ["## Summary", "  |  ".join(summary_parts), ""]

    # ── Broken ────────────────────────────────────────────────────────────────
    if broken:
        lines += ["## 🔴 Broken Tests", ""]
        for b in broken[:20]:  # cap at 20 to keep report readable
            name = _short_name(b["test_id"])
            evidence = (
                f"Failed {b['consecutive']}/{b['total_checked']} recent runs "
                f"— last failure {b['last_ts']}"
            )
            lines.append(f"- **`{name}`**")
            lines.append(f"  {evidence}")
        lines.append("")

    # ── Flaky ─────────────────────────────────────────────────────────────────
    if flaky:
        lines += ["## 🟡 Flaky Tests", ""]
        for fl in flaky[:20]:
            name = _short_name(fl["test_id"])
            dates_str = ", ".join(fl["failure_dates"])
            evidence = (
                f"Failed {fl['fail_count']}/{fl['window']} recent runs "
                f"({fl['rate_pct']}%) — failures on {dates_str}"
            )
            lines.append(f"- **`{name}`**")
            lines.append(f"  {evidence}")
        lines.append("")

    # ── Slow ──────────────────────────────────────────────────────────────────
    if slow:
        lines += ["## 🐌 Slow Tests", ""]
        for s in slow[:15]:
            name = _short_name(s["test_id"])
            parts: list[str] = [f"avg {s['mean_recent_s']}s (last {min(s['run_count'], _SLOW_RECENT_WINDOW)} runs)"]
            if s["is_trending"] and s["trend_factor"]:
                parts.append(f"↑ {s['trend_factor']}× vs baseline")
            parts.append(f"worst {s['worst_s']}s")
            evidence = " · ".join(parts)
            lines.append(f"- **`{name}`**")
            lines.append(f"  {evidence}")
        lines.append("")

    # ── Footer ────────────────────────────────────────────────────────────────
    lines += [
        "---",
        f"_Broken = ≥{_BROKEN_MIN_CONSECUTIVE} consecutive failures · "
        f"Flaky = {int(_FLAKY_MIN_FAILURE_RATE*100)}–{int(_FLAKY_MAX_FAILURE_RATE*100)}% failure rate in last {_WINDOW_RECENT} runs · "
        f"Slow = avg ≥{_SLOW_ABSOLUTE_S}s or {int(_SLOW_TREND_FACTOR)}× trend_",
    ]
    return "\n".join(lines)


def run(print_output: bool = False) -> str:
    history = _load_history()
    if not history:
        msg = "# 🧪 Test Health Report\n\n_No test history yet — run the test suite first._\n"
        _REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
        _REPORT_FILE.write_text(msg)
        if print_output:
            print(msg)
        return msg

    broken, flaky, slow = _analyse(history)
    report = _render_report(history, broken, flaky, slow)
    _REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    _REPORT_FILE.write_text(report)
    if print_output:
        print(report)
    return report


if __name__ == "__main__":
    run(print_output="--print" in sys.argv)
