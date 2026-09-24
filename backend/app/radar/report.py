"""Генератор отчёта .md по рилсу — перенос Радар/backend/main.py::_build_report_md.

Единственное сознательное отличие от оригинала: заголовок транскрипта раньше был жёстко
«## Транскрипт (Whisper)», а в ReelScribe расшифровку теперь чаще делает Gemini (один вызов
на t/v/tv, см. docs/specs/08-radar.md) — движок берём из radar_analyses.transcript_engine и
подписываем только если это реально whisper, иначе просто «## Транскрипт».
"""
from __future__ import annotations

from typing import Any, Optional


def build_report_md(reel: dict[str, Any], analysis: Optional[dict[str, Any]]) -> str:
    an = analysis or {}

    views = reel.get("views") or 0
    likes = reel.get("likes") or 0
    comments_n = reel.get("comments") or 0
    er = round((likes + comments_n) / views * 100, 2) if views else 0
    dur = reel.get("duration_sec") or 0

    lines = [
        f"# @{reel.get('username')} — {reel.get('id')}",
        "",
        f"**Просмотры:** {views:,}  |  **ER:** {er}%  |  **Лайки:** {likes:,}  |  **Комментарии:** {comments_n:,}",
        f"**Длительность:** {int(dur)} сек  |  **Ссылка:** {reel.get('url', '')}",
        "",
    ]

    segs = an.get("transcript_segments")
    if segs:
        title = "## Транскрипт (Whisper)" if an.get("transcript_engine") == "whisper" else "## Транскрипт"
        lines.append(title)
        for seg in segs:
            s = seg.get("start", 0)
            m, sec = divmod(int(s), 60)
            lines.append(f"{m}:{sec:02d} — {seg.get('text', '')}")
        lines.append("")

    vt = an.get("visual_timeline")
    if vt:
        lines.append("## Что в кадре (Gemini)")
        for item in vt:
            lines.append(f"**{item.get('t', '')}** — {item.get('text', '')}")
        lines.append("")

    if an.get("hook"):
        lines.append(f"## Хук\n{an['hook']}\n")
    if an.get("structure"):
        lines.append(f"## Структура\n{an['structure']}\n")
    if an.get("cta"):
        lines.append(f"## CTA\n{an['cta']}\n")
    if an.get("format_idea"):
        lines.append(f"## Идея формата\n{an['format_idea']}\n")

    return "\n".join(lines)
