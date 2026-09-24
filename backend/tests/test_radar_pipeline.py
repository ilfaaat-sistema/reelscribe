"""Тесты разбора рилса (ТЗ 08 «Радар», агент B): захват заданий, отмена, rate-limit,
валидация таймкодов Gemini, очистка временной папки. Никаких реальных сетевых вызовов —
Apify/httpx/Gemini/Supabase замоканы.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.radar import downloader, gemini, pipeline, whisper


def run_async(coro):
    """pytest-asyncio в проекте не заведён (см. requirements.txt/CLAUDE.md — минимум
    зависимостей) — гоняем корутины через обычный asyncio.run, без плагина и маркеров."""
    return asyncio.run(coro)


# ── Мини-фейк supabase-py: только то подмножество fluent-API, которое реально
# используется в pipeline.py/downloader.py (table/select/update/upsert/eq/lte/lt/in_/
# order/limit/execute). Хранилище — dict[table_name] -> list[dict], строки-ссылки, чтобы
# update() мутировал их на месте, как настоящая база. ──────────────────────────────────


class _FakeResponse:
    def __init__(self, data: list[dict]):
        self.data = data


class _FakeQuery:
    def __init__(self, table: _FakeTable, op: str, payload: dict | None = None):
        self._table = table
        self._op = op
        self._payload = payload
        self._filters: list[tuple[str, str, object]] = []
        self._order = None
        self._limit = None
        self._on_conflict = None

    def eq(self, col, val):
        self._filters.append(("eq", col, val))
        return self

    def lte(self, col, val):
        self._filters.append(("lte", col, val))
        return self

    def lt(self, col, val):
        self._filters.append(("lt", col, val))
        return self

    def in_(self, col, vals):
        self._filters.append(("in", col, vals))
        return self

    def neq(self, col, val):
        self._filters.append(("neq", col, val))
        return self

    def order(self, col, **_kwargs):
        self._order = col
        return self

    def limit(self, n):
        self._limit = n
        return self

    @staticmethod
    def _passes(op: str, rv: object, val: object) -> bool:
        if op == "eq":
            return rv == val
        if op == "lte":
            return rv is not None and rv <= val
        if op == "lt":
            return rv is not None and rv < val
        if op == "in":
            return rv in val
        if op == "neq":
            return rv != val
        raise ValueError(f"неизвестный оператор фильтра: {op}")

    def _match(self) -> list[dict]:
        out = []
        for row in self._table.rows:
            if all(self._passes(op, row.get(col), val) for op, col, val in self._filters):
                out.append(row)
        if self._order:
            out = sorted(out, key=lambda r: r.get(self._order) or "")
        if self._limit is not None:
            out = out[: self._limit]
        return out

    def execute(self) -> _FakeResponse:
        if self._op == "insert":
            new_row = dict(self._payload)
            self._table.rows.append(new_row)
            return _FakeResponse([new_row])

        if self._op == "upsert":
            key = self._on_conflict
            existing = None
            if key:
                for row in self._table.rows:
                    if row.get(key) == self._payload.get(key):
                        existing = row
                        break
            if existing is not None:
                existing.update(self._payload)
                return _FakeResponse([existing])
            new_row = dict(self._payload)
            self._table.rows.append(new_row)
            return _FakeResponse([new_row])

        matched = self._match()
        if self._op == "update":
            for row in matched:
                row.update(self._payload)
            return _FakeResponse(matched)
        return _FakeResponse(matched)


class _FakeTable:
    def __init__(self, name: str, store: dict):
        self.name = name
        self.store = store

    @property
    def rows(self) -> list[dict]:
        return self.store.setdefault(self.name, [])

    def select(self, *_args, **_kwargs):
        return _FakeQuery(self, "select")

    def update(self, payload: dict):
        return _FakeQuery(self, "update", payload)

    def insert(self, payload: dict):
        return _FakeQuery(self, "insert", payload)

    def upsert(self, payload: dict, on_conflict: str | None = None):
        q = _FakeQuery(self, "upsert", payload)
        q._on_conflict = on_conflict
        return q


class FakeDB:
    def __init__(self):
        self.store: dict[str, list[dict]] = {}

    def table(self, name: str) -> _FakeTable:
        return _FakeTable(name, self.store)


# ── Хелперы для заполнения фейковой базы ────────────────────────────────────────────────


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def seed_job(db: FakeDB, **overrides) -> dict:
    now_iso = _iso(_now())
    job = {
        "id": "job-1",
        "reel_id": "REEL1",
        "mode": "tv",
        "state": "queued",
        "attempts": 0,
        "next_attempt_at": now_iso,
        "error": None,
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    job.update(overrides)
    db.table("radar_jobs").rows.append(job)
    return job


def seed_reel(db: FakeDB, reel_id: str = "REEL1", **overrides) -> dict:
    reel = {
        "id": reel_id,
        "username": "someone",
        "url": f"https://www.instagram.com/reel/{reel_id}/",
        "video_url": "https://cdn.example.com/video.mp4?oe=ffffffff",
        "duration_sec": 30.0,
    }
    reel.update(overrides)
    db.table("radar_reels").rows.append(reel)
    return reel


@pytest.fixture()
def fake_db(monkeypatch) -> FakeDB:
    db = FakeDB()
    monkeypatch.setattr(pipeline, "get_db", lambda: db)
    monkeypatch.setattr(downloader, "get_db", lambda: db)
    return db


# ── link_expired ─────────────────────────────────────────────────────────────────────


def test_link_expired_none_url():
    assert downloader.link_expired(None) is True


def test_link_expired_past_oe():
    past = int((_now() - timedelta(hours=1)).timestamp())
    url = f"https://cdn.example.com/v.mp4?oe={past:x}"
    assert downloader.link_expired(url) is True


def test_link_expired_within_margin():
    # Через 5 минут — меньше 10-минутного запаса, тоже считаем протухшей.
    soon = int((_now() + timedelta(minutes=5)).timestamp())
    url = f"https://cdn.example.com/v.mp4?oe={soon:x}"
    assert downloader.link_expired(url) is True


def test_link_expired_far_future_oe():
    far = int((_now() + timedelta(hours=6)).timestamp())
    url = f"https://cdn.example.com/v.mp4?oe={far:x}"
    assert downloader.link_expired(url) is False


def test_link_expired_no_oe_param_considered_alive():
    assert downloader.link_expired("https://cdn.example.com/v.mp4") is False


# ── Захват задания: атомарность ─────────────────────────────────────────────────────────


def test_claim_job_second_call_returns_none(fake_db):
    seed_job(fake_db, id="job-1", reel_id="REEL1", attempts=0)

    first = pipeline.claim_job("REEL1")
    assert first is not None
    assert first["state"] == "in_progress"
    assert first["attempts"] == 1

    second = pipeline.claim_job("REEL1")
    assert second is None  # второй "одновременный" вызов уже не находит queued-строку


def test_claim_job_future_next_attempt_returns_none(fake_db):
    future = _iso(_now() + timedelta(seconds=60))
    seed_job(fake_db, next_attempt_at=future)

    assert pipeline.claim_job("REEL1") is None


def test_claim_job_no_job_returns_none(fake_db):
    assert pipeline.claim_job("MISSING") is None


def test_claim_job_exhausted_attempts_marks_failed_and_returns_none(fake_db):
    from app.radar import settings

    # 429 возвращал задание в queued без проверки MAX_ATTEMPTS — повторялось бесконечно.
    seed_job(fake_db, attempts=settings.MAX_ATTEMPTS)
    seed_reel(fake_db)

    claimed = pipeline.claim_job("REEL1")

    assert claimed is None  # задание с исчерпанным лимитом не отдаётся на обработку
    job_row = fake_db.store["radar_jobs"][0]
    assert job_row["state"] == "failed"
    assert "попыт" in job_row["error"].lower()
    analysis = fake_db.store["radar_analyses"][0]
    assert analysis["status"] == "error"


# ── Страховка pg_cron: claim_stale_job ──────────────────────────────────────────────────


def test_claim_stale_job_picks_stuck_queued(fake_db):
    from app.radar import settings

    stuck_created = _iso(_now() - timedelta(seconds=settings.STALE_QUEUED_SEC + 30))
    seed_job(fake_db, id="job-stuck", created_at=stuck_created)

    claimed = pipeline.claim_stale_job()

    assert claimed is not None
    assert claimed["state"] == "in_progress"
    assert claimed["attempts"] == 1


def test_claim_stale_job_ignores_fresh_queued(fake_db):
    seed_job(fake_db)  # created_at = сейчас, не старше STALE_QUEUED_SEC

    assert pipeline.claim_stale_job() is None


def test_claim_stale_job_reclaims_stuck_running_below_max_attempts(fake_db):
    from app.radar import settings

    stuck_updated = _iso(_now() - timedelta(seconds=settings.STALE_RUNNING_SEC + 30))
    seed_job(fake_db, id="job-running", state="in_progress", attempts=1, updated_at=stuck_updated)

    claimed = pipeline.claim_stale_job()

    assert claimed is not None
    assert claimed["state"] == "in_progress"
    assert claimed["attempts"] == 2


def test_claim_stale_job_marks_failed_after_max_attempts(fake_db):
    from app.radar import settings

    stuck_updated = _iso(_now() - timedelta(seconds=settings.STALE_RUNNING_SEC + 30))
    seed_job(
        fake_db,
        id="job-exhausted",
        state="in_progress",
        attempts=settings.MAX_ATTEMPTS,
        updated_at=stuck_updated,
    )
    seed_reel(fake_db)

    claimed = pipeline.claim_stale_job()

    assert claimed is None  # проваленное задание не отдаётся на обработку
    job_row = fake_db.store["radar_jobs"][0]
    assert job_row["state"] == "failed"
    analysis = fake_db.store["radar_analyses"][0]
    assert analysis["status"] == "error"


def test_claim_stale_job_returns_none_when_nothing_stuck(fake_db):
    assert pipeline.claim_stale_job() is None


# ── Отмена ────────────────────────────────────────────────────────────────────────────


def test_process_radar_job_cancelled_before_start(fake_db):
    job = seed_job(fake_db, state="cancelled")
    seed_reel(fake_db)

    status = run_async(pipeline.process_radar_job(job))

    assert status == "cancelled"
    analysis = fake_db.store["radar_analyses"][0]
    assert analysis["status"] == "cancelled"
    job_row = fake_db.store["radar_jobs"][0]
    assert job_row["state"] == "cancelled"


# ── Rate limit ────────────────────────────────────────────────────────────────────────


def test_process_radar_job_rate_limited(fake_db, monkeypatch, tmp_path):
    job = seed_job(fake_db, state="in_progress", attempts=1)
    seed_reel(fake_db)

    async def fake_download_to(tmpdir, reel):
        p = Path(tmpdir) / f"{reel['id']}.mp4"
        p.write_bytes(b"fake-mp4")
        return p

    def fake_analyze(video_path, mode, duration_sec=None):
        raise gemini.RateLimitedError("429 RESOURCE_EXHAUSTED")

    monkeypatch.setattr(pipeline.downloader, "download_to", fake_download_to)
    monkeypatch.setattr(pipeline.gemini, "analyze", fake_analyze)

    before = _now()
    status = run_async(pipeline.process_radar_job(job))
    assert status == "rate_limited"

    job_row = fake_db.store["radar_jobs"][0]
    assert job_row["state"] == "queued"
    next_attempt = datetime.fromisoformat(job_row["next_attempt_at"])
    assert next_attempt >= before + timedelta(seconds=55)  # ~60с, с запасом на выполнение теста

    analysis = fake_db.store["radar_analyses"][0]
    assert analysis["status"] == "rate_limited"


def test_process_radar_job_cancelled_during_rate_limit_stays_cancelled(fake_db, monkeypatch):
    # Гонка: пользователь нажал «отменить», пока шёл вызов Gemini, который в итоге упал 429.
    # Раньше _finish_rate_limited безусловно возвращал job в queued поверх отмены — задание
    # оживало и tick снова платил за него.
    job = seed_job(fake_db, state="in_progress", attempts=1)
    seed_reel(fake_db)

    async def fake_download_to(tmpdir, reel):
        p = Path(tmpdir) / f"{reel['id']}.mp4"
        p.write_bytes(b"fake-mp4")
        return p

    def fake_analyze(video_path, mode, duration_sec=None):
        fake_db.store["radar_jobs"][0]["state"] = "cancelled"  # отмена подоспела во время вызова
        raise gemini.RateLimitedError("429 RESOURCE_EXHAUSTED")

    monkeypatch.setattr(pipeline.downloader, "download_to", fake_download_to)
    monkeypatch.setattr(pipeline.gemini, "analyze", fake_analyze)

    status = run_async(pipeline.process_radar_job(job))

    assert status == "cancelled"
    job_row = fake_db.store["radar_jobs"][0]
    assert job_row["state"] == "cancelled"  # не перезаписано обратно в queued
    analysis = fake_db.store["radar_analyses"][0]
    assert analysis["status"] == "cancelled"


# ── Успех и ошибка — временная папка не остаётся ────────────────────────────────────────


def test_process_radar_job_success_cleans_tmpdir(fake_db, monkeypatch):
    job = seed_job(fake_db)
    seed_reel(fake_db)

    captured: dict = {}

    async def fake_download_to(tmpdir, reel):
        captured["tmpdir"] = tmpdir
        p = Path(tmpdir) / f"{reel['id']}.mp4"
        p.write_bytes(b"fake-mp4")
        return p

    def fake_analyze(video_path, mode, duration_sec=None):
        assert os.path.exists(video_path)
        return {
            "transcript_segments": [{"start": 0.0, "end": 1.5, "text": "привет"}],
            "transcript_language": "ru",
            "visual_timeline": [{"t": "0:00-0:02", "text": "человек говорит в камеру"}],
            "hook": "яркая заставка",
            "structure": "хук 0-2с, блок 2-20с, cta 20-30с",
            "cta": "подписывайся",
            "format_idea": "адаптировать под нишу фитнеса",
        }

    monkeypatch.setattr(pipeline.downloader, "download_to", fake_download_to)
    monkeypatch.setattr(pipeline.gemini, "analyze", fake_analyze)

    status = run_async(pipeline.process_radar_job(job))

    assert status == "done"
    assert not os.path.exists(captured["tmpdir"])  # TemporaryDirectory убрал за собой

    job_row = fake_db.store["radar_jobs"][0]
    assert job_row["state"] == "done"
    analysis = fake_db.store["radar_analyses"][0]
    assert analysis["status"] == "done"
    assert analysis["transcript_engine"] == "gemini"
    assert analysis["hook"] == "яркая заставка"


def test_process_radar_job_v_after_t_preserves_transcript_and_sets_mode_tv(fake_db, monkeypatch):
    # Уже есть готовая расшифровка от предыдущего запуска mode='t'; следующим запускается
    # mode='v' — не должен затирать её None-ами, а итоговый mode строки должен стать 'tv'.
    fake_db.store["radar_analyses"] = [
        {
            "reel_id": "REEL1",
            "mode": "t",
            "status": "done",
            "error": None,
            "updated_at": _iso(_now()),
            "transcript_segments": [{"start": 0.0, "end": 1.0, "text": "было"}],
            "transcript_language": "ru",
            "transcript_engine": "gemini",
            "visual_timeline": None,
            "hook": None,
            "structure": None,
            "cta": None,
            "format_idea": None,
        }
    ]
    job = seed_job(fake_db, mode="v")
    seed_reel(fake_db)

    async def fake_download_to(tmpdir, reel):
        p = Path(tmpdir) / f"{reel['id']}.mp4"
        p.write_bytes(b"fake-mp4")
        return p

    def fake_analyze(video_path, mode, duration_sec=None):
        assert mode == "v"
        return {
            "visual_timeline": [{"t": "0:00-0:02", "text": "новое"}],
            "hook": "хук",
            "structure": "структура",
            "cta": "подписывайся",
            "format_idea": "идея",
        }

    monkeypatch.setattr(pipeline.downloader, "download_to", fake_download_to)
    monkeypatch.setattr(pipeline.gemini, "analyze", fake_analyze)

    status = run_async(pipeline.process_radar_job(job))

    assert status == "done"
    analysis = fake_db.store["radar_analyses"][0]
    assert analysis["mode"] == "tv"
    assert analysis["transcript_segments"] == [{"start": 0.0, "end": 1.0, "text": "было"}]  # не стёрлось
    assert analysis["transcript_language"] == "ru"
    assert analysis["visual_timeline"] == [{"t": "0:00-0:02", "text": "новое"}]
    assert analysis["hook"] == "хук"


def test_process_radar_job_error_cleans_tmpdir(fake_db, monkeypatch):
    job = seed_job(fake_db)
    seed_reel(fake_db)

    captured: dict = {}

    async def fake_download_to(tmpdir, reel):
        captured["tmpdir"] = tmpdir
        p = Path(tmpdir) / f"{reel['id']}.mp4"
        p.write_bytes(b"fake-mp4")
        return p

    def fake_analyze(video_path, mode, duration_sec=None):
        raise RuntimeError("Gemini недоступен")

    monkeypatch.setattr(pipeline.downloader, "download_to", fake_download_to)
    monkeypatch.setattr(pipeline.gemini, "analyze", fake_analyze)

    status = run_async(pipeline.process_radar_job(job))

    assert status == "error"
    assert not os.path.exists(captured["tmpdir"])

    job_row = fake_db.store["radar_jobs"][0]
    assert job_row["state"] == "failed"
    assert "Gemini недоступен" in job_row["error"]
    analysis = fake_db.store["radar_analyses"][0]
    assert analysis["status"] == "error"


def test_process_radar_job_download_error(fake_db, monkeypatch):
    job = seed_job(fake_db)
    seed_reel(fake_db)

    async def fake_download_to(tmpdir, reel):
        raise downloader.DownloadError("ссылка на видео недействительна (HTTP 403)")

    monkeypatch.setattr(pipeline.downloader, "download_to", fake_download_to)

    status = run_async(pipeline.process_radar_job(job))

    assert status == "error"
    analysis = fake_db.store["radar_analyses"][0]
    assert "недействительна" in analysis["error"]


# ── Transcriber='whisper' (ТЗ 08, итерация 2) ────────────────────────────────────────────


def test_process_radar_job_whisper_mode_t_gemini_not_called(fake_db, monkeypatch):
    job = seed_job(fake_db, mode="t", transcriber="whisper")
    seed_reel(fake_db)

    async def fake_download_to(tmpdir, reel):
        p = Path(tmpdir) / f"{reel['id']}.mp4"
        p.write_bytes(b"fake-mp4")
        return p

    def fake_whisper_transcribe(video_path):
        assert os.path.exists(video_path)
        return [{"start": 0.0, "end": 1.2, "text": "привет"}], "ru"

    def fail_gemini_analyze(*a, **kw):
        raise AssertionError("Gemini не должен вызываться для transcriber='whisper', mode='t'")

    monkeypatch.setattr(pipeline.downloader, "download_to", fake_download_to)
    monkeypatch.setattr(pipeline.whisper, "transcribe", fake_whisper_transcribe)
    monkeypatch.setattr(pipeline.gemini, "analyze", fail_gemini_analyze)

    status = run_async(pipeline.process_radar_job(job))

    assert status == "done"
    analysis = fake_db.store["radar_analyses"][0]
    assert analysis["transcript_engine"] == "openai-whisper"
    assert analysis["transcript_segments"] == [{"start": 0.0, "end": 1.2, "text": "привет"}]
    assert analysis["transcript_language"] == "ru"
    assert analysis.get("visual_timeline") is None  # mode='t' — визуальные поля не пишутся вовсе
    assert analysis["mode"] == "t"


def test_process_radar_job_whisper_mode_tv_merges_whisper_and_gemini_visual(fake_db, monkeypatch):
    # tv + whisper: расшифровку делает Whisper, видеоряд — отдельным вызовом Gemini в mode='v'
    # (без повторной расшифровки речи Gemini-ем), результаты сливаются в одну строку.
    job = seed_job(fake_db, mode="tv", transcriber="whisper")
    seed_reel(fake_db)

    async def fake_download_to(tmpdir, reel):
        p = Path(tmpdir) / f"{reel['id']}.mp4"
        p.write_bytes(b"fake-mp4")
        return p

    def fake_whisper_transcribe(video_path):
        return [{"start": 0.0, "end": 1.2, "text": "привет"}], "ru"

    def fake_gemini_analyze(video_path, mode, duration_sec=None):
        assert mode == "v"
        return {
            "transcript_segments": None,
            "transcript_language": None,
            "visual_timeline": [{"t": "0:00-0:02", "text": "человек говорит"}],
            "hook": "хук",
            "structure": "структура",
            "cta": "подписывайся",
            "format_idea": "идея",
        }

    monkeypatch.setattr(pipeline.downloader, "download_to", fake_download_to)
    monkeypatch.setattr(pipeline.whisper, "transcribe", fake_whisper_transcribe)
    monkeypatch.setattr(pipeline.gemini, "analyze", fake_gemini_analyze)

    status = run_async(pipeline.process_radar_job(job))

    assert status == "done"
    analysis = fake_db.store["radar_analyses"][0]
    assert analysis["mode"] == "tv"
    assert analysis["transcript_engine"] == "openai-whisper"
    assert analysis["transcript_segments"] == [{"start": 0.0, "end": 1.2, "text": "привет"}]
    assert analysis["transcript_language"] == "ru"
    assert analysis["visual_timeline"] == [{"t": "0:00-0:02", "text": "человек говорит"}]
    assert analysis["hook"] == "хук"


def test_process_radar_job_whisper_rate_limited_returns_to_queue(fake_db, monkeypatch):
    job = seed_job(fake_db, mode="t", transcriber="whisper")
    seed_reel(fake_db)

    async def fake_download_to(tmpdir, reel):
        p = Path(tmpdir) / f"{reel['id']}.mp4"
        p.write_bytes(b"fake-mp4")
        return p

    def fake_whisper_transcribe(video_path):
        raise gemini.RateLimitedError("OpenAI Whisper: 429")

    monkeypatch.setattr(pipeline.downloader, "download_to", fake_download_to)
    monkeypatch.setattr(pipeline.whisper, "transcribe", fake_whisper_transcribe)

    status = run_async(pipeline.process_radar_job(job))

    assert status == "rate_limited"
    job_row = fake_db.store["radar_jobs"][0]
    assert job_row["state"] == "queued"
    analysis = fake_db.store["radar_analyses"][0]
    assert analysis["status"] == "rate_limited"


def test_process_radar_job_default_transcriber_is_gemini(fake_db, monkeypatch):
    # Задание без поля transcriber (старые строки/страховка pg_cron) — ведёт себя как раньше.
    job = seed_job(fake_db, mode="t")
    job.pop("transcriber", None)
    seed_reel(fake_db)

    async def fake_download_to(tmpdir, reel):
        p = Path(tmpdir) / f"{reel['id']}.mp4"
        p.write_bytes(b"fake-mp4")
        return p

    def fail_whisper_transcribe(video_path):
        raise AssertionError("Whisper не должен вызываться, если transcriber не 'whisper'")

    def fake_gemini_analyze(video_path, mode, duration_sec=None):
        assert mode == "t"
        return {
            "transcript_segments": [{"start": 0.0, "end": 1.0, "text": "текст"}],
            "transcript_language": "ru",
            "visual_timeline": None, "hook": None, "structure": None, "cta": None, "format_idea": None,
        }

    monkeypatch.setattr(pipeline.downloader, "download_to", fake_download_to)
    monkeypatch.setattr(pipeline.whisper, "transcribe", fail_whisper_transcribe)
    monkeypatch.setattr(pipeline.gemini, "analyze", fake_gemini_analyze)

    status = run_async(pipeline.process_radar_job(job))

    assert status == "done"
    analysis = fake_db.store["radar_analyses"][0]
    assert analysis["transcript_engine"] == "gemini"


# ── whisper.py: вызов OpenAI напрямую (без сети — httpx.post замокан) ───────────────────


class _FakeHttpResponse:
    def __init__(self, status_code: int, json_data: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text or str(json_data)

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("error", request=None, response=self)


def test_whisper_transcribe_no_key_raises(monkeypatch, tmp_path):
    from app.radar import settings as radar_settings

    monkeypatch.setattr(radar_settings, "OPENAI_API_KEY", "")
    video = tmp_path / "v.mp4"
    video.write_bytes(b"fake")

    with pytest.raises(whisper.WhisperUnavailableError):
        whisper.transcribe(video)


def test_whisper_transcribe_file_too_large_raises(monkeypatch, tmp_path):
    from app.radar import settings as radar_settings

    monkeypatch.setattr(radar_settings, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(radar_settings, "WHISPER_MAX_BYTES", 10)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 20)

    with pytest.raises(whisper.WhisperFileTooLargeError):
        whisper.transcribe(video)


def test_whisper_transcribe_parses_verbose_json(monkeypatch, tmp_path):
    from app.radar import settings as radar_settings

    monkeypatch.setattr(radar_settings, "OPENAI_API_KEY", "sk-test")
    video = tmp_path / "v.mp4"
    video.write_bytes(b"fake-mp4")

    captured = {}

    def fake_post(url, headers=None, files=None, data=None, timeout=None):
        captured.update(url=url, headers=headers, data=data)
        return _FakeHttpResponse(200, {
            "task": "transcribe",
            "language": "russian",
            "duration": 3.2,
            "text": "привет мир",
            "segments": [
                {"id": 0, "start": 0.0, "end": 1.5, "text": " привет ", "seek": 0, "tokens": [], "temperature": 0.0, "avg_logprob": -0.1, "compression_ratio": 1.0, "no_speech_prob": 0.01},
                {"id": 1, "start": 1.5, "end": 3.2, "text": " мир ", "seek": 0, "tokens": [], "temperature": 0.0, "avg_logprob": -0.1, "compression_ratio": 1.0, "no_speech_prob": 0.01},
            ],
        })

    monkeypatch.setattr(whisper.httpx, "post", fake_post)

    segments, language = whisper.transcribe(video)

    assert segments == [
        {"start": 0.0, "end": 1.5, "text": "привет"},
        {"start": 1.5, "end": 3.2, "text": "мир"},
    ]
    assert language == "russian"
    assert captured["data"]["model"] == radar_settings.WHISPER_MODEL
    assert captured["data"]["response_format"] == "verbose_json"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"


def test_whisper_transcribe_429_raises_rate_limited(monkeypatch, tmp_path):
    from app.radar import settings as radar_settings

    monkeypatch.setattr(radar_settings, "OPENAI_API_KEY", "sk-test")
    video = tmp_path / "v.mp4"
    video.write_bytes(b"fake-mp4")

    def fake_post(url, headers=None, files=None, data=None, timeout=None):
        return _FakeHttpResponse(429, text="rate limited")

    monkeypatch.setattr(whisper.httpx, "post", fake_post)

    with pytest.raises(gemini.RateLimitedError):
        whisper.transcribe(video)


def test_whisper_transcribe_other_http_error_raises_whisper_error(monkeypatch, tmp_path):
    from app.radar import settings as radar_settings

    monkeypatch.setattr(radar_settings, "OPENAI_API_KEY", "sk-test")
    video = tmp_path / "v.mp4"
    video.write_bytes(b"fake-mp4")

    def fake_post(url, headers=None, files=None, data=None, timeout=None):
        return _FakeHttpResponse(500, text="server error")

    monkeypatch.setattr(whisper.httpx, "post", fake_post)

    with pytest.raises(whisper.WhisperError):
        whisper.transcribe(video)


# ── Валидация сегментов расшифровки (gemini.py) ──────────────────────────────────────────


def _schema(**overrides):
    payload = {
        "transcript_segments": [],
        "transcript_language": "ru",
        "visual_timeline": [],
        "hook": "",
        "structure": "",
        "cta": "",
        "format_idea": "",
        "video_duration_sec": 30.0,
    }
    payload.update(overrides)
    return gemini._AnalysisSchema(**payload)


def test_validate_ascending_segments_ok():
    result = _schema(
        transcript_segments=[
            {"start": 0.0, "end": 1.5, "text": "a"},
            {"start": 1.5, "end": 3.0, "text": "b"},
        ]
    )
    gemini._validate(result, "tv")  # не должно бросать


def test_validate_segments_out_of_order_raises():
    result = _schema(
        transcript_segments=[
            {"start": 5.0, "end": 6.0, "text": "a"},
            {"start": 2.0, "end": 3.0, "text": "b"},
        ]
    )
    with pytest.raises(gemini.ValidationError):
        gemini._validate(result, "tv")


def test_validate_segment_beyond_duration_raises():
    result = _schema(
        video_duration_sec=10.0,
        transcript_segments=[{"start": 0.0, "end": 15.0, "text": "a"}],
    )
    with pytest.raises(gemini.ValidationError):
        gemini._validate(result, "tv")


def test_validate_segment_within_tolerance_ok():
    # duration=10, end=10.9 — в пределах допуска "+1с" из ТЗ
    result = _schema(
        video_duration_sec=10.0,
        transcript_segments=[{"start": 0.0, "end": 10.9, "text": "a"}],
    )
    gemini._validate(result, "tv")


def test_validate_skipped_for_video_only_mode():
    # mode='v' — расшифровка не запрашивалась, сегменты не проверяются
    result = _schema(
        video_duration_sec=1.0,
        transcript_segments=[{"start": 100.0, "end": 50.0, "text": "битые данные"}],
    )
    gemini._validate(result, "v")  # не должно бросать — сегменты для этого mode не смотрим


def test_to_contract_dict_mode_t_has_no_visual_fields():
    result = _schema(
        transcript_segments=[{"start": 0.0, "end": 1.0, "text": "a"}],
        hook="не должно попасть в ответ",
    )
    out = gemini._to_contract_dict(result, "t")
    assert out["transcript_segments"] == [{"start": 0.0, "end": 1.0, "text": "a"}]
    assert out["hook"] is None
    assert out["visual_timeline"] is None


def test_to_contract_dict_mode_v_has_no_transcript_fields():
    result = _schema(hook="удерживает", visual_timeline=[{"t": "0:00-0:02", "text": "x"}])
    out = gemini._to_contract_dict(result, "v")
    assert out["transcript_segments"] is None
    assert out["transcript_language"] is None
    assert out["hook"] == "удерживает"
