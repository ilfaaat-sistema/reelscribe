"""Тесты ИИ-чата (ТЗ 09, `docs/specs/09-ai-chat.md`).

Два независимых блока:
- `search_text` — против ЖИВОЙ базы (read-only, ничего не пишет), критерий приёмки №1 из ТЗ.
- Цикл `agent.run_agent` — Gemini полностью замокан (никаких сетевых вызовов), проверяет, что
  инструмент вызывается, ответ собирается из финального текста, а `reels` фильтруются по тому,
  что модель реально упомянула.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.chat import agent, tools

# ── search_text против живой базы ────────────────────────────────────────────

def test_search_text_finds_hermes_reel():
    """Критерий приёмки ТЗ 09 №1: паттерн с вариантами написания находит DaFYHA0PqTN / @yanosh_iii."""
    results = tools.search_text("herm[eè]s|гермес|эрмес|хермес")

    assert results, "search_text ничего не нашёл на живой базе"
    matches = [r for r in results if r.get("shortcode") == "DaFYHA0PqTN"]
    assert matches, f"DaFYHA0PqTN не найден среди результатов: {results}"
    assert matches[0]["author"] == "yanosh_iii"
    assert matches[0]["source"] == "parser"
    assert matches[0]["snippet"]  # сниппет вырезан вокруг совпадения, не пустой


def test_search_text_respects_fields_filter():
    """Ограничение полей сужает поиск — по caption_ru не находится то, что есть только в caption."""
    all_fields = tools.search_text("гермес", limit=50)
    only_summary = tools.search_text("гермес", fields=["summary"], limit=50)
    assert all(r.get("field") == "transcripts.summary" for r in only_summary if "error" not in r)
    assert len(all_fields) >= len(only_summary)


def test_search_text_empty_pattern_no_match_is_safe():
    """Паттерн без совпадений не должен падать — просто пустой список."""
    results = tools.search_text("совершенно-невозможное-слово-xyzzy-000")
    assert results == []


def test_search_text_date_filter_and_sort_by_er():
    """Живой прогон вскрыл отказ модели («не могу искать и фильтровать по дате одновременно»).

    search_text должен уметь текстовый поиск + фильтр по дате + сортировку ОДНИМ вызовом:
    embed-фильтр `reels!inner(...)` реально отсекает строки transcripts вне периода (не просто
    обнуляет вложенный reels), а сортировка по залётности идёт по убыванию после объединения
    источников.
    """
    results = tools.search_text(
        "claude|клод", date_from="2026-08-01", date_to="2026-08-31", sort_by="er", limit=50,
    )
    assert results, "по паттерну claude|клод в августе 2026 ничего не нашлось на живой базе"

    for r in results:
        if r.get("error"):
            continue
        posted_at = r.get("posted_at")
        assert posted_at and posted_at[:7] == "2026-08", f"embed-фильтр пропустил рилс вне августа: {r}"

    ers = [r["er"] for r in results if not r.get("error") and r.get("er") is not None]
    assert ers == sorted(ers, reverse=True), "результат не отсортирован по залётности по убыванию"


# ── Дедуп: один рилс, найденный через несколько полей, — одна запись ────────────────────────
# Юнит-тест на моке db (не на живой базе): нужен детерминированный случай, когда один и тот же
# id реально приходит от нескольких полей сразу, а живые данные такого совпадения сейчас не дают
# (проверено вручную — ни один рилс не совпадает по «гермес» больше чем в одном поле).

class _FakeQueryResp:
    def __init__(self, data: list[dict[str, Any]]):
        self.data = data


class _FakeQuery:
    """Заглушка чейна postgrest-py: игнорирует аргументы фильтров, отдаёт фиксированный список."""

    def __init__(self, data: list[dict[str, Any]]):
        self._data = data

    def select(self, *a, **k):
        return self

    def filter(self, *a, **k):
        return self

    def gte(self, *a, **k):
        return self

    def lte(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        return _FakeQueryResp(self._data)


class _FakeDB:
    def __init__(self, table_data: dict[str, list[dict[str, Any]]]):
        self._table_data = table_data

    def table(self, name: str):
        return _FakeQuery(self._table_data.get(name, []))


def test_search_text_dedup_same_reel_across_fields(monkeypatch):
    """Один и тот же рилс, совпавший сразу в нескольких полях, возвращается одной записью."""
    reel_row = {
        "id": 555, "shortcode": "DEDUP1", "url": "https://www.instagram.com/reel/DEDUP1/",
        "author_handle": "author1", "views": 1000, "likes": 50, "er": 3.0,
        "posted_at": "2026-05-01", "caption": "рассказ про Гермес", "caption_ru": "рассказ про Гермес",
    }
    transcript_row = {
        "reel_id": 555,
        "text": "рассказ про Гермес", "text_ru": "рассказ про Гермес",
        "summary": "рассказ про Гермес", "ocr_text": "рассказ про Гермес",
        "reels": reel_row,
    }
    fake_db = _FakeDB({"reels": [reel_row], "transcripts": [transcript_row], "radar_reels": []})
    monkeypatch.setattr(tools, "get_db", lambda: fake_db)

    results = tools.search_text("гермес", limit=50)

    matches = [r for r in results if r.get("shortcode") == "DEDUP1"]
    assert len(matches) == 1, f"один рилс вернулся несколько раз вместо одной записи: {matches}"
    # Побеждает первое поле по порядку _FIELD_SPECS (reels.caption раньше transcripts.*).
    assert matches[0]["field"] == "reels.caption"


# ── Сниппет: обрезка по границам слов, не с середины ─────────────────────────────────────────

def test_word_boundary_start_snaps_back_to_space():
    text = "привет мир как дела"
    idx = text.index("мир") + 1  # внутри слова «мир»
    assert tools._word_boundary_start(text, idx) == text.index("мир")


def test_word_boundary_end_snaps_forward_to_space():
    text = "привет мир как дела"
    idx = text.index("мир") + 1  # внутри слова «мир»
    assert tools._word_boundary_end(text, idx) == text.index("мир") + len("мир")


def test_make_snippet_no_mid_word_cut_and_has_ellipsis():
    """Баг живого прогона: сниппет начинался с середины слова («логии» вместо «технологии»)."""
    left = "слово " * 40  # заведомо длиннее SNIPPET_RADIUS (120 символов) слева от совпадения
    right = " слово" * 40
    text = f"{left}технологиями{right}"

    snippet = tools._make_snippet(text, "технологиями")

    assert snippet is not None
    assert snippet.startswith("…") and snippet.endswith("…"), snippet
    core = snippet[1:-1]
    assert "технологиями" in snippet  # ключевое слово вошло в сниппет целиком, не обрублено

    start_idx = text.index(core)
    end_idx = start_idx + len(core)
    assert start_idx == 0 or text[start_idx - 1].isspace(), f"левый край режет слово: {snippet!r}"
    assert end_idx == len(text) or text[end_idx].isspace(), f"правый край режет слово: {snippet!r}"


# ── Цикл агента на моке Gemini ───────────────────────────────────────────────

class _FakeFunctionCall:
    def __init__(self, name: str, args: dict[str, Any], call_id: str = "call-1"):
        self.name = name
        self.args = args
        self.id = call_id


class _FakePart:
    def __init__(self, function_call: _FakeFunctionCall | None = None, text: str | None = None):
        self.function_call = function_call
        self.text = text


class _FakeResponse:
    def __init__(self, parts: list[_FakePart], text: str | None = None):
        content = SimpleNamespace(parts=parts, role="model")
        self.candidates = [SimpleNamespace(content=content)]
        self.text = text
        self.usage_metadata = SimpleNamespace(
            prompt_token_count=1200, candidates_token_count=80, thoughts_token_count=0,
        )


class _FakeModels:
    def __init__(self, responses: list[_FakeResponse]):
        self._responses = list(responses)
        self.calls = 0

    def generate_content(self, *, model, contents, config):
        self.calls += 1
        return self._responses.pop(0)


class _FakeClient:
    def __init__(self, responses: list[_FakeResponse]):
        self.models = _FakeModels(responses)


def _fake_search_result() -> list[dict[str, Any]]:
    return [
        {
            "source": "parser", "id": "e6be9b85-8990-4577-b0ba-771a4c9d5709",
            "shortcode": "DaFYHA0PqTN", "url": "https://www.instagram.com/reel/DaFYHA0PqTN/",
            "author": "yanosh_iii", "views": 348510, "er": 62.27, "posted_at": "2026-06-27",
            "field": "reels.caption", "snippet": "Сегодня тестировал Ray-Ban с Гермесом в гараже",
        },
        {
            # Инструмент вернул это тоже, но модель на него в ответе НЕ сослалась —
            # в финальный reels попасть не должен.
            "source": "parser", "id": "0a8d15d7-ae4a-42b9-a375-0992ecf1b75d",
            "shortcode": "DZKJ0MLxvoe", "url": "https://www.instagram.com/reel/DZKJ0MLxvoe/",
            "author": "maxtreysi", "views": 140415, "er": 0.57, "posted_at": "2026-06-04",
            "field": "reels.caption", "snippet": "агента на базе Hermes",
        },
    ]


def test_agent_tool_call_then_final_answer_filters_reels(monkeypatch):
    """Вызов инструмента → финальный ответ → reels отфильтрованы по тому, что упомянуто в тексте."""
    call_part = _FakePart(
        function_call=_FakeFunctionCall("search_text", {"pattern": "herm[eè]s|гермес"}),
    )
    step1 = _FakeResponse(parts=[call_part])

    answer = (
        "Про очки Ray-Ban с Гермесом рассказывал @yanosh_iii (DaFYHA0PqTN), просмотры 348510."
    )
    step2 = _FakePart(text=answer)
    final = _FakeResponse(parts=[step2], text=answer)

    fake_client = _FakeClient([step1, final])
    monkeypatch.setattr(agent, "_client", lambda: fake_client)
    monkeypatch.setattr(
        agent, "TOOL_FUNCTIONS",
        {**agent.TOOL_FUNCTIONS, "search_text": lambda **kwargs: _fake_search_result()},
    )

    result = agent.run_agent("кто рассказывал про очки с Гермесом", history=[])

    assert result["answer"] == answer
    assert fake_client.models.calls == 2  # один вызов инструмента + один финальный

    reels = result["reels"]
    assert len(reels) == 1
    assert reels[0]["shortcode"] == "DaFYHA0PqTN"
    assert reels[0]["author"] == "yanosh_iii"
    assert reels[0]["source"] == "parser"

    usage = result["usage"]
    assert usage["steps"] == 2
    assert usage["input_tokens"] == 2400  # 1200 * 2 шага
    assert usage["output_tokens"] == 160
    assert usage["cost_rub"] > 0


def test_agent_forces_final_answer_after_max_steps(monkeypatch):
    """Модель зациклилась на вызовах инструмента — на 6-м шаге функции выключены, ответ обязателен."""
    call_part = _FakePart(function_call=_FakeFunctionCall("stats", {"group_by": "author"}))
    loop_steps = [_FakeResponse(parts=[call_part]) for _ in range(5)]

    final_text = "Не удалось получить точный ответ за отведённые шаги."
    final_part = _FakePart(text=final_text)
    final_step = _FakeResponse(parts=[final_part], text=final_text)

    fake_client = _FakeClient([*loop_steps, final_step])
    monkeypatch.setattr(agent, "_client", lambda: fake_client)
    monkeypatch.setattr(
        agent, "TOOL_FUNCTIONS", {**agent.TOOL_FUNCTIONS, "stats": lambda **kwargs: []},
    )

    result = agent.run_agent("бесконечный вопрос", history=[])

    assert result["answer"] == final_text
    assert result["usage"]["steps"] == agent.MAX_STEPS
    assert fake_client.models.calls == agent.MAX_STEPS


def test_agent_wraps_gemini_error(monkeypatch):
    """Ошибка клиента Gemini превращается в ChatAgentError (api/chat.py делает из неё 502)."""

    class _Boom:
        class models:
            @staticmethod
            def generate_content(**kwargs):
                raise RuntimeError("503 UNAVAILABLE\nподробности")

    monkeypatch.setattr(agent, "_client", lambda: _Boom())

    with pytest.raises(agent.ChatAgentError) as exc_info:
        agent.run_agent("вопрос", history=[])
    assert "503 UNAVAILABLE" in str(exc_info.value)
