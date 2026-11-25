#!/usr/bin/env python3
"""Sync WakaTime coding & AI metrics into README.md using Python stdlib only."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
from urllib.parse import quote
import urllib.request

WAKATIME_BASE_URL = "https://wakatime.com/api/v1/users/current"
USER_AGENT = "qwerprog-profile-sync/1.0"
SECTION_PATTERN = re.compile(
    r"(<!--START_SECTION:waka-->\r?\n).*?(<!--END_SECTION:waka-->)",
    re.DOTALL,
)
CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


class WakaTimeAuthError(Exception):
    """Raised when WakaTime rejects credentials (HTTP 401/403)."""


def sanitize_text(value: object) -> str:
    """Strip control characters, newlines, and backticks from external strings."""
    if value is None:
        return ""
    cleaned = str(value).replace("`", "")
    cleaned = CONTROL_CHARS_RE.sub(" ", cleaned)
    return " ".join(cleaned.split())


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _format_percent(value: float, decimals: int = 2) -> str:
    formatted = f"{round(value, decimals):.{decimals}f}"
    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")
    return formatted or "0"


def _quote_badge_segment(text: str) -> str:
    # Escape Shields.io path separators ('-' and '_') before URL encoding.
    escaped = text.replace("-", "--").replace("_", "__")
    return quote(escaped, safe="")


def make_graph(percent: float) -> str:
    """Render a 25-character Unicode progress bar."""
    clamped = max(0.0, min(100.0, percent))
    done = max(0, min(25, round(clamped / 4.0)))
    return "█" * done + "░" * (25 - done)


def find_category(categories: object, name: str) -> dict | None:
    """Find a matching category dict by exact name."""
    if not isinstance(categories, list):
        return None
    for item in categories:
        if isinstance(item, dict) and item.get("name") == name:
            return item
    return None


def format_bars(items: object, top_n: int = 5) -> str:
    """Format top N stat entries as aligned text progress bars."""
    if not isinstance(items, list) or not items:
        return "No Activity Tracked This Week"

    valid_items = [item for item in items if isinstance(item, dict)]
    if not valid_items:
        return "No Activity Tracked This Week"

    sorted_items = sorted(
        valid_items,
        key=lambda item: _safe_float(item.get("percent", 0.0)),
        reverse=True,
    )[:top_n]

    lines: list[str] = []
    for item in sorted_items:
        name = sanitize_text(item.get("name", "Unknown"))[:24]
        text = sanitize_text(item.get("text", "0 secs"))[:19]
        pct = max(0.0, min(100.0, _safe_float(item.get("percent", 0.0))))
        lines.append(f"{name:<25}{text:<20}{make_graph(pct)}   {pct:05.2f} %")
    return "\n".join(lines)


def fetch_wakatime_json(
    endpoint_path: str,
    token: str,
    *,
    max_attempts: int = 5,
    retry_delay: float = 15.0,
    timeout: float = 15.0,
) -> dict | None:
    """Fetch a WakaTime JSON endpoint with Bearer auth and 201/202/transient retries.

    Returns None when stats are temporarily unavailable (e.g. HTTP 201/202, 429,
    5xx, or network timeout) so callers can preserve existing README content.
    Raises WakaTimeAuthError on HTTP 401/403.
    """
    clean_path = endpoint_path.lstrip("/")
    url = f"{WAKATIME_BASE_URL}/{clean_path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }

    for attempt in range(1, max_attempts + 1):
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
                raw = resp.read().decode("utf-8", errors="replace")

            if status == 200:
                payload = json.loads(raw)
                if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
                    return payload
                print(
                    f"Attempt {attempt}/{max_attempts} for {clean_path}: invalid JSON structure",
                    file=sys.stderr,
                )
            elif status in (201, 202):
                print(
                    f"Attempt {attempt}/{max_attempts} for {clean_path}: HTTP {status} (calculating stats)",
                    file=sys.stderr,
                )
            else:
                print(
                    f"Attempt {attempt}/{max_attempts} for {clean_path}: unexpected HTTP {status}",
                    file=sys.stderr,
                )
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            if status in (401, 403):
                print(
                    f"::error::WakaTime authentication failed (HTTP {status}) on {clean_path}.",
                    file=sys.stderr,
                )
                raise WakaTimeAuthError(f"HTTP {status} on {clean_path}") from exc
            if status == 429 or status >= 500:
                print(
                    f"Attempt {attempt}/{max_attempts} for {clean_path}: transient HTTP {status}",
                    file=sys.stderr,
                )
            else:
                print(
                    f"::warning::WakaTime request {clean_path} returned HTTP {status}; skipping update.",
                    file=sys.stderr,
                )
                return None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            print(
                f"Attempt {attempt}/{max_attempts} for {clean_path}: {type(exc).__name__}",
                file=sys.stderr,
            )

        if attempt < max_attempts:
            time.sleep(retry_delay)

    return None


def _render_ai_coding_insights(
    ai_written_percent: float,
    prompt_length_avg: float,
    prompts_per_session: float,
    manual_touch_percent: float,
) -> str:
    if ai_written_percent >= 66:
        reliance_label = "🤖 AI-Driven"
    elif ai_written_percent >= 33:
        reliance_label = "⚖️ Balanced"
    else:
        reliance_label = "🧑‍💻 Mostly Hands-On"

    if prompt_length_avg > 1500:
        prompt_label = "📚 Verbose Prompter"
    elif prompt_length_avg >= 500:
        prompt_label = "📄 Detailed Prompter"
    else:
        prompt_label = "📝 Concise Prompter"

    session_label = "🔁 Iterative Prompter" if prompts_per_session > 1.5 else "🎯 One-Shot Prompter"
    review_label = "👀 Hands-On Reviewer" if manual_touch_percent >= 50 else "🚀 High AI Trust"

    lines = [
        "🔎 AI Coding Insights:",
        f"{reliance_label} — {_format_percent(ai_written_percent)}% of written lines came from AI",
        f"{prompt_label} — average {round(prompt_length_avg):,} characters per prompt",
        f"{session_label} — average {_format_percent(prompts_per_session, decimals=1)} prompts per session",
        f"{review_label} — {_format_percent(manual_touch_percent)}% of changed lines were hand-edited",
    ]
    return "\n".join(lines)


def _render_ai_coding_block(weekly_data: dict) -> str:
    ai_cat = find_category(weekly_data.get("categories"), "AI Coding")
    ai_sessions = _safe_int(weekly_data.get("ai_sessions", 0))

    if ai_cat is None or ai_sessions <= 0:
        return "🤖 **AI Coding This Week**\n\n```text\nNo AI Coding Activity Tracked This Week\n```"

    ai_additions = _safe_int(weekly_data.get("ai_additions", 0))
    ai_deletions = _safe_int(weekly_data.get("ai_deletions", 0))
    human_additions = _safe_int(weekly_data.get("human_additions", 0))
    human_deletions = _safe_int(weekly_data.get("human_deletions", 0))
    ai_input_tokens = _safe_int(weekly_data.get("ai_input_tokens", 0))
    ai_output_tokens = _safe_int(weekly_data.get("ai_output_tokens", 0))
    ai_prompts = _safe_int(weekly_data.get("ai_prompt_events_total", 0))
    prompt_length_avg = _safe_float(weekly_data.get("ai_prompt_length_avg", 0.0))
    prompts_per_session = _safe_float(weekly_data.get("ai_prompt_events_avg_per_session", 0.0))

    total_additions = ai_additions + human_additions
    ai_written_percent = (ai_additions / total_additions * 100.0) if total_additions else 0.0

    total_changes = ai_additions + ai_deletions + human_additions + human_deletions
    manual_touch_percent = ((human_additions + human_deletions) / total_changes * 100.0) if total_changes else 0.0

    ai_time_text = sanitize_text(ai_cat.get("text")) or "0 secs"
    ai_time_pct = _format_percent(_safe_float(ai_cat.get("percent", 0.0)))

    # Omit estimated AI cost line for privacy minimization.
    parts = [
        f"⏱ AI Coding Time: {ai_time_text} ({ai_time_pct}%)",
        f"✍️ {ai_additions:,} lines written by AI, {human_additions:,} lines written by hand ({_format_percent(ai_written_percent)}% AI-written)",
        f"🔤 {ai_input_tokens:,} Input Tokens, {ai_output_tokens:,} Output Tokens",
        f"🧠 {ai_sessions:,} AI Sessions, {ai_prompts:,} AI Prompts",
    ]

    raw_models = weekly_data.get("ai_model_breakdown")
    if isinstance(raw_models, list) and raw_models:
        models = [m for m in raw_models if isinstance(m, dict)]
        if models:
            total_lines = sum(max(0, _safe_int(m.get("lines", 0))) for m in models) or 1
            model_items = [
                {
                    "name": sanitize_text(m.get("name", "Unknown")),
                    "text": f"{max(0, _safe_int(m.get('lines', 0))):,} lines",
                    "percent": max(0, _safe_int(m.get("lines", 0))) / total_lines * 100.0,
                }
                for m in models
            ]
            parts.append(format_bars(model_items, top_n=len(model_items)))

    parts.append(
        _render_ai_coding_insights(
            ai_written_percent,
            prompt_length_avg,
            prompts_per_session,
            manual_touch_percent,
        )
    )

    body = "\n\n".join(parts)
    return f"🤖 **AI Coding This Week**\n\n```text\n{body}\n```"


def render_wakatime_section(
    weekly_payload: dict,
    all_time_payload: dict,
    *,
    now: datetime | None = None,
) -> str:
    """Render the complete WakaTime Markdown block for README.md."""
    weekly_data = weekly_payload.get("data", {})
    all_time_data = all_time_payload.get("data", {})

    badges: list[str] = []
    total_time = sanitize_text(all_time_data.get("human_readable_total"))
    if total_time:
        badges.append(
            f"![Code Time](https://img.shields.io/badge/Code%20Time-{_quote_badge_segment(total_time)}-blue?style=flat-square)"
        )

    ai_all_cat = find_category(all_time_data.get("categories"), "AI Coding")
    if ai_all_cat is not None:
        ai_all_text = sanitize_text(ai_all_cat.get("text"))
        if ai_all_text:
            badges.append(
                f"![AI Code Time](https://img.shields.io/badge/AI%20Code%20Time-{_quote_badge_segment(ai_all_text)}-blue?style=flat-square)"
            )

    tz = sanitize_text(weekly_data.get("timezone")) or "UTC"
    weekly_parts = [
        f"🕑︎ Time Zone: {tz}",
        f"💬 Programming Languages:\n{format_bars(weekly_data.get('languages'))}",
        f"🔥 Editors:\n{format_bars(weekly_data.get('editors'))}",
        f"💻 Operating System:\n{format_bars(weekly_data.get('operating_systems'))}",
    ]
    weekly_block = "📊 **This Week I Spent My Time On**\n\n```text\n" + "\n\n".join(weekly_parts) + "\n```"

    ai_block = _render_ai_coding_block(weekly_data)

    ts = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    updated_line = f"Last Updated on {ts.strftime('%d/%m/%Y %H:%M:%S')} UTC"

    sections: list[str] = []
    if badges:
        sections.append("\n".join(badges))
    sections.extend([weekly_block, ai_block, updated_line])
    return "\n\n".join(sections)


def update_readme_file(readme_path: Path, rendered: str) -> bool:
    """Replace the WakaTime section inside README.md safely; return True if changed."""
    original = readme_path.read_text(encoding="utf-8")
    match = SECTION_PATTERN.search(original)
    if match is None:
        raise ValueError("Missing <!--START_SECTION:waka--> / <!--END_SECTION:waka--> markers in README.md")

    newline = "\r\n" if "\r\n" in match.group(1) else "\n"
    normalized_rendered = rendered.replace("\r\n", "\n").replace("\n", newline)
    updated = SECTION_PATTERN.sub(
        lambda m: f"{m.group(1)}{normalized_rendered}{newline}{m.group(2)}",
        original,
        count=1,
    )
    if updated == original:
        return False

    readme_path.write_text(updated, encoding="utf-8")
    return True


def main(readme_path: Path | None = None, api_key: str | None = None) -> int:
    """Entry point for syncing WakaTime stats into README.md."""
    token = (api_key if api_key is not None else os.environ.get("WAKATIME_API_KEY", "")).strip()
    if not token:
        print("::error::Missing WAKATIME_API_KEY environment variable.", file=sys.stderr)
        return 1

    target_readme = readme_path or Path("README.md")
    if not target_readme.is_file():
        print(f"::error::Target README file not found: {target_readme}", file=sys.stderr)
        return 1

    try:
        weekly_payload = fetch_wakatime_json("stats/last_7_days", token)
        if weekly_payload is None:
            print(
                "::warning::WakaTime stats not ready (non-200); preserving existing README metrics.",
                file=sys.stderr,
            )
            return 0

        all_time_payload = fetch_wakatime_json("stats/all_time", token)
        if all_time_payload is None:
            print(
                "::warning::WakaTime stats not ready (non-200); preserving existing README metrics.",
                file=sys.stderr,
            )
            return 0
    except WakaTimeAuthError:
        return 1

    rendered = render_wakatime_section(weekly_payload, all_time_payload)
    try:
        changed = update_readme_file(target_readme, rendered)
    except ValueError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    print(f"WakaTime section {'updated' if changed else 'unchanged'} in {target_readme}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
