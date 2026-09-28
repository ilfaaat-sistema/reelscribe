"""Цикл ИИ-агента ИИ-чата (ТЗ 09) — ручной function calling поверх `gemini-2.5-flash`.

SDK и подход к клиенту сверены с `app/radar/gemini.py` (тот же `google-genai==1.47.0`,
тот же `GEMINI_API_KEY`, без Vertex). Отличие: там один вызов с response_schema, здесь —
цикл до 6 шагов с ручным диспетчером функций (`app/chat/tools.py`), потому что модели нужно
самой решать, каким инструментом и сколько раз воспользоваться.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

from app.chat.prompts import SYSTEM_PROMPT
from app.chat.tools import TOOL_FUNCTIONS, TOOL_SCHEMAS
from app.radar import settings as radar_settings

logger = logging.getLogger(__name__)

# Тот же ключ, что у Радара (app/radar/gemini.py) — просьба ТЗ 09, свой GEMINI_API_KEY не заводим.
GEMINI_API_KEY = radar_settings.GEMINI_API_KEY
CHAT_GEMINI_MODEL = os.getenv("CHAT_GEMINI_MODEL", "gemini-2.5-flash")
THINKING_BUDGET = 512

MAX_STEPS = 6
MAX_REELS_IN_ANSWER = 12

# Тариф flash (курс 80 ₽/$, см. корневой CLAUDE.md «Суммы в рублях»).
USD_PER_1M_INPUT = 0.30
USD_PER_1M_OUTPUT = 2.50  # мысли считаются по тарифу выхода
RUB_PER_USD = 80


class ChatAgentError(Exception):
    """Gemini недоступен/ответил ошибкой — api/chat.py превращает это в 502."""


def _client():
    if not GEMINI_API_KEY:
        raise ChatAgentError("Не задан GEMINI_API_KEY — чат недоступен")
    from google import genai

    return genai.Client(api_key=GEMINI_API_KEY)


def _build_tool():
    from google.genai import types

    declarations = [
        types.FunctionDeclaration(
            name=spec["name"],
            description=spec["description"],
            parametersJsonSchema=spec["parameters"],
        )
        for spec in TOOL_SCHEMAS
    ]
    return types.Tool(functionDeclarations=declarations)


def _history_to_contents(history: list[dict[str, str]]):
    from google.genai import types

    contents = []
    for item in history:
        role = "model" if item.get("role") == "assistant" else "user"
        text = item.get("text") or ""
        if not text:
            continue
        contents.append(types.Content(role=role, parts=[types.Part(text=text)]))
    return contents


def _extract_text(response) -> str:
    text = getattr(response, "text", None)
    if text:
        return text
    parts = []
    candidates = getattr(response, "candidates", None) or []
    if candidates and candidates[0].content and candidates[0].content.parts:
        for p in candidates[0].content.parts:
            if getattr(p, "text", None):
                parts.append(p.text)
    return "".join(parts)


def _call_tool(name: str, args: dict[str, Any]) -> Any:
    fn = TOOL_FUNCTIONS.get(name)
    if fn is None:
        return {"error": f"неизвестный инструмент {name}"}
    try:
        return fn(**(args or {}))
    except Exception as exc:  # noqa: BLE001 — ошибка инструмента уходит модели, а не роняет чат
        logger.warning("Инструмент %s упал: %s", name, exc)
        return {"error": f"инструмент {name} не выполнен: {exc}"}


def _collect_reel_cards(tool_name: str, result: Any, sink: dict[tuple, dict]) -> None:
    """Складывает записи рилсов из результата инструмента в общий словарь по (source, id)."""
    items: list[Any]
    if isinstance(result, list):
        items = result
    elif isinstance(result, dict):
        items = [result]
    else:
        return

    for item in items:
        if not isinstance(item, dict) or item.get("error"):
            continue
        source = item.get("source")
        item_id = item.get("id")
        if not source or item_id is None:
            continue  # например, строки stats() — не карточка рилса
        key = (source, str(item_id))
        if key in sink:
            continue
        if tool_name == "get_reel":
            snippet = (item.get("caption_ru") or item.get("caption") or item.get("transcript") or "")
            snippet = snippet[:300] or None
            author = item.get("author_handle")
        else:
            snippet = item.get("snippet")
            author = item.get("author")
        sink[key] = {
            "id": item_id,
            "source": source,
            "shortcode": item.get("shortcode"),
            "url": item.get("url"),
            "author": author,
            "views": item.get("views"),
            "er": item.get("er"),
            "posted_at": item.get("posted_at"),
            "snippet": snippet,
        }


def _reels_mentioned_in_answer(cards: dict[tuple, dict], answer_text: str) -> list[dict]:
    """Только рилсы, на которые модель реально сослалась (shortcode или @автор в тексте)."""
    if not answer_text:
        return []
    found: list[tuple[int, dict]] = []
    for card in cards.values():
        idx = None
        shortcode = card.get("shortcode")
        if shortcode and shortcode in answer_text:
            idx = answer_text.find(shortcode)
        if idx is None and card.get("author"):
            author = card["author"]
            m = re.search(re.escape(author), answer_text, re.IGNORECASE)
            if m:
                idx = m.start()
        if idx is not None:
            found.append((idx, card))
    found.sort(key=lambda pair: pair[0])
    return [card for _, card in found[:MAX_REELS_IN_ANSWER]]


def _accumulate_usage(response, usage: dict[str, int]) -> None:
    meta = getattr(response, "usage_metadata", None)
    if meta is None:
        return
    usage["input_tokens"] += meta.prompt_token_count or 0
    usage["output_tokens"] += meta.candidates_token_count or 0
    usage["thinking_tokens"] += meta.thoughts_token_count or 0


def _cost_rub(usage: dict[str, int]) -> float:
    cost_usd = (
        usage["input_tokens"] / 1_000_000 * USD_PER_1M_INPUT
        + (usage["output_tokens"] + usage["thinking_tokens"]) / 1_000_000 * USD_PER_1M_OUTPUT
    )
    return round(cost_usd * RUB_PER_USD, 2)


def run_agent(message: str, history: list[dict[str, str]] | None = None) -> dict[str, Any]:
    """Один вопрос → ответ модели по контракту `POST /api/chat` (см. docs/specs/09-ai-chat.md).

    Возвращает {"answer", "reels", "usage"}. Бросает `ChatAgentError` при недоступности Gemini —
    api/chat.py превращает её в 502.
    """
    from google.genai import types

    client = _client()
    tool = _build_tool()

    contents = _history_to_contents(history or [])
    contents.append(types.Content(role="user", parts=[types.Part(text=message)]))

    base_config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=[tool],
        thinking_config=types.ThinkingConfig(thinking_budget=THINKING_BUDGET),
    )
    final_config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        thinking_config=types.ThinkingConfig(thinking_budget=THINKING_BUDGET),
        tool_config=types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(mode="NONE")
        ),
    )

    usage = {"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0}
    reel_cards: dict[tuple, dict] = {}
    tools_called: list[str] = []
    answer_text = ""
    steps_used = 0

    t0 = time.monotonic()
    try:
        for step in range(1, MAX_STEPS + 1):
            steps_used = step
            force_final = step == MAX_STEPS
            config = final_config if force_final else base_config

            response = client.models.generate_content(
                model=CHAT_GEMINI_MODEL, contents=contents, config=config,
            )
            _accumulate_usage(response, usage)

            candidates = response.candidates or []
            parts = candidates[0].content.parts if candidates and candidates[0].content else []
            function_calls = [p.function_call for p in (parts or []) if p.function_call]

            if function_calls and not force_final:
                contents.append(candidates[0].content)
                response_parts = []
                for fc in function_calls:
                    tools_called.append(fc.name)
                    result = _call_tool(fc.name, fc.args or {})
                    _collect_reel_cards(fc.name, result, reel_cards)
                    fr = types.FunctionResponse(
                        name=fc.name, response={"result": result}, id=fc.id,
                    )
                    response_parts.append(types.Part(function_response=fr))
                contents.append(types.Content(role="user", parts=response_parts))
                continue

            answer_text = _extract_text(response)
            break
    except ChatAgentError:
        raise
    except Exception as exc:
        first_line = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
        raise ChatAgentError(first_line) from exc

    reels = _reels_mentioned_in_answer(reel_cards, answer_text)
    cost_rub = _cost_rub(usage)

    logger.info(
        "chat: шагов=%d инструменты=%s токены(вход/выход/мысли)=%d/%d/%d стоимость=%.2f₽ время=%.1fс",
        steps_used, tools_called, usage["input_tokens"], usage["output_tokens"],
        usage["thinking_tokens"], cost_rub, time.monotonic() - t0,
    )

    return {
        "answer": answer_text,
        "reels": reels,
        "usage": {
            "steps": steps_used,
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
            "thinking_tokens": usage["thinking_tokens"],
            "cost_rub": cost_rub,
        },
    }
