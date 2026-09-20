"""Разметка рилсов по происхождению: учётка Instagram + сохранённая папка / директ.

Берёт папку выгрузки архива Instagram (`Настройки → Загрузить информацию`) одной учётки и
проставляет уже существующим в базе рилсам метки в `reel_sources` (учётка, папка/директ), а для
рилсов, которых в базе ещё нет, — сначала заводит их через тот же путь, что и обычный импорт
(`_setup_jobs` из `import_service.py`), не дублируя логику дедупа по shortcode.

Источники внутри папки экспорта:
  - `saved/saved_collections.json` — сохранённые ПАПКИ. Форма экспорта негарантирована (Meta меняет
    схему год от года): в текущем экспорте (сентябрь 2026) `media` у каждой коллекции пустой, а
    реальные ссылки лежат ГЛУБОКО во вложенной группе `label_values` с `title == 'Медиафайлы'`
    (в mojibake). Поэтому здесь не хардкодится путь до ссылок — обход рекурсивный, ищет любые
    строки-инстаграм-ссылки внутри объекта коллекции, откуда бы они ни взялись.
  - `saved/saved_posts.json` — плоский журнал ВСЕХ сохранений (в папках и вне папок) с timestamp
    сохранения каждого поста. Используется только чтобы подтащить `added_at` тем рилсам, что нашлись
    в папках (сама выборка папок не отсюда).
  - `messages/**/*.json` — переписка, через готовую `parse_message_json()`. Метка `kind='direct'`,
    `name='Директ'`.

ВАЖНО (обнаружено на реальных данных 20.09.2026): имена сохранённых папок могут повторяться
НЕ ТОЛЬКО между учётками, но и ВНУТРИ одной — Instagram допускает две разные физические коллекции
с одинаковым именем (напр. в `ilfaaat_sistema` две разные коллекции называются «Нейросети»:
127 + 34 медиа). Модель БД метит по имени (`reel_sources.name`), а не по внутреннему id коллекции —
такие коллекции сознательно схлопываются в одну метку, это следствие принятой в ТЗ 06 схемы, а не
баг обхода.

Три режима (по возрастанию последствий):
  1. `--dry-run` — ничего не пишет в БД вообще, только считает и сохраняет отчёт на диск.
  2. `--labels-only` — проставляет метки в `reel_sources` ТОЛЬКО тем shortcode, что уже есть в
     `reels`; недостающие ничего не создаёт (ни `reels`, ни `transcripts`, ни `jobs` — деньги
     владельца не тратятся), а просто считает их и перечисляет в отчёте. Разметка существующего
     бесплатна (это SQL), поэтому идёт сразу, без отдельного подтверждения.
  3. Без флагов — полный прогон: то же, что `--labels-only`, плюс недостающие рилсы заводятся и
     ставятся в очередь на скачивание/распознавание через `_setup_jobs` (**платно**, см. §8 ТЗ 06).
     Запускается только после явного «да» владельца с цифрой в рублях (её даёт `--dry-run`).

Запуск:
    # 1. сначала всегда так — ничего не пишет в БД, только считает и сохраняет отчёт на диск
    python -m app.workers.import_saved --root "<путь к выгрузке>" --account ilfaaat_sistema \
        --folders "Нейросети,3д,Нейросети для фото и видео,Повторить про нейросети,Создание сайтов" \
        --dry-run --out /tmp/report.json

    python -m app.workers.import_saved --root "<путь к выгрузке>" --account neyro_set7 \
        --folders all --direct --dry-run --out /tmp/report2.json

    # 2. разметить то, что уже в базе, — бесплатно, ничего нового не создаёт
    python -m app.workers.import_saved --root "<путь>" --account ilfaaat_sistema \
        --folders "Нейросети,3д,Нейросети для фото и видео,Повторить про нейросети,Создание сайтов" \
        --direct --labels-only

    # 3. боевой прогон одной папки (создаёт недостающие рилсы — платно)
    python -m app.workers.import_saved --root "<путь>" --account ilfaaat_sistema --folders "3д"
"""
from __future__ import annotations

import argparse
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.core.db import get_db
from app.models.schemas import ImportRequest
from app.pipeline.normalize import ParsedReel, _fix_mojibake, parse_message_json
from app.services.import_service import _setup_jobs

logger = logging.getLogger(__name__)

_RE_URL = re.compile(
    r'instagram\.com/(?P<type>reel|p|tv)/(?P<sc>[A-Za-z0-9_-]+)',
    re.IGNORECASE,
)

_COST_PER_REEL_USD = 0.0047
_RUB_PER_USD = 80

# Значения, которыми в разных языках/версиях экспорта Meta подписывает поле «имя коллекции».
_NAME_LABELS = {'Имя', 'Name', 'имя'}


@dataclass
class SourceItem:
    shortcode: str
    type: str
    url: str
    added_at: str | None = None  # ISO8601, если известно время сохранения


def _reel_type(kind: str) -> str:
    kind = kind.lower()
    if kind == 'reel':
        return 'reel'
    if kind == 'tv':
        return 'tv'
    return 'post'


def _extract_reels(node: object) -> dict:
    """Рекурсивно находит все ссылки на рилсы/посты внутри произвольного JSON-объекта.

    Не завязан на конкретный путь/ключ — только на то, что ссылка это строка с 'instagram.com'.
    Значения могут быть в mojibake, поэтому чиним перед матчем регулярки.
    """
    found: dict = {}

    def walk(o: object) -> None:
        if isinstance(o, dict):
            for v in o.values():
                if isinstance(v, str) and 'instagram.com' in v:
                    m = _RE_URL.search(_fix_mojibake(v))
                    if m:
                        sc = m.group('sc')
                        kind = m.group('type')
                        found.setdefault(sc, SourceItem(
                            shortcode=sc,
                            type=_reel_type(kind),
                            url=f'https://www.instagram.com/{kind.lower()}/{sc}/',
                        ))
                else:
                    walk(v)
        elif isinstance(o, list):
            for item in o:
                walk(item)

    walk(node)
    return found


def _collection_name(collection: dict) -> str | None:
    """Имя папки из плоских (не вложенных) label_values, с починкой mojibake и strip()."""
    for item in collection.get('label_values', []):
        if 'dict' in item:
            continue
        label = item.get('label')
        value = item.get('value')
        if isinstance(label, str) and isinstance(value, str) and _fix_mojibake(label) in _NAME_LABELS:
            return _fix_mojibake(value).strip()
    return None


def parse_saved_collections(path: Path, only: set | None) -> dict:
    """{имя_папки: {shortcode: SourceItem}}. `only=None` — взять все найденные папки."""
    data = json.loads(path.read_bytes().decode('utf-8', errors='replace'))
    if not isinstance(data, list):
        logger.warning('%s: неожиданный формат (не список) — пропускаю', path)
        return {}

    result: dict = {}
    for collection in data:
        if not isinstance(collection, dict):
            continue
        name = _collection_name(collection)
        if not name:
            continue
        if only is not None and name not in only:
            continue
        items = _extract_reels(collection.get('label_values', []))
        if not items:
            continue
        bucket = result.setdefault(name, {})
        bucket.update(items)
    return result


def _load_saved_timestamps(path: Path) -> dict:
    """shortcode → unix-timestamp сохранения, из плоского журнала saved_posts.json."""
    if not path.exists():
        return {}
    data = json.loads(path.read_bytes().decode('utf-8', errors='replace'))
    if not isinstance(data, list):
        return {}
    ts_by_sc: dict = {}
    for post in data:
        if not isinstance(post, dict):
            continue
        ts = post.get('timestamp')
        items = _extract_reels(post.get('label_values', []))
        for sc in items:
            if isinstance(ts, int):
                ts_by_sc.setdefault(sc, ts)
    return ts_by_sc


def parse_direct_messages(root: Path) -> dict:
    """{shortcode: SourceItem} по всем messages/**/*.json под корнем выгрузки учётки."""
    result: dict = {}
    for fp in sorted(root.glob('messages/**/*.json')):
        try:
            parsed = parse_message_json(fp.read_bytes())
        except Exception:
            logger.exception('%s: не удалось разобрать', fp)
            continue
        for p in parsed:
            result.setdefault(p.shortcode, SourceItem(shortcode=p.shortcode, type=p.type, url=p.url))
    return result


def _iso(ts: int | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def build_registry(root: Path, folders_arg: str | None, direct: bool) -> dict:
    """Возвращает {(kind, name): {shortcode: SourceItem}}."""
    sources: dict = {}

    if folders_arg is not None:
        collections_path = root / 'saved' / 'saved_collections.json'
        if not collections_path.exists():
            logger.warning('%s не найден — папки не размечаю', collections_path)
        else:
            only = None if folders_arg.strip().lower() == 'all' else {
                f.strip() for f in folders_arg.split(',') if f.strip()
            }
            folders = parse_saved_collections(collections_path, only)

            if only is not None:
                missing = only - set(folders.keys())
                for name in missing:
                    logger.warning('Папка %r не найдена в выгрузке (или в ней нет ссылок)', name)

            ts_by_sc = _load_saved_timestamps(root / 'saved' / 'saved_posts.json')
            for name, items in folders.items():
                for sc, item in items.items():
                    if sc in ts_by_sc:
                        item.added_at = _iso(ts_by_sc[sc])
                sources[('folder', name)] = items

    if direct:
        sources[('direct', 'Директ')] = parse_direct_messages(root)

    return sources


def _chunks(seq: list, size: int) -> list:
    return [seq[i:i + size] for i in range(0, len(seq), size)]


def _lookup_reel_ids(db, shortcodes: list) -> dict:
    """shortcode → reel_id (uuid) для тех, что уже есть в базе."""
    result: dict = {}
    for chunk in _chunks(shortcodes, 200):
        if not chunk:
            continue
        rows = db.table('reels').select('id, shortcode').in_('shortcode', chunk).execute().data
        for r in rows:
            result[r['shortcode']] = r['id']
    return result


def _default_import_request(account: str) -> ImportRequest:
    """Те же дефолты, что и в обычном импорте через UI (см. frontend/src/pages/Import.jsx)."""
    return ImportRequest(
        links_text='',
        source_type='json',
        engine='faster-whisper',
        model='medium',
        translate=True,
        pull_stats=True,
        comment=f'saved-import: {account}',
    )


def make_report(sources: dict, account: str, sc_to_id: dict) -> dict:
    groups = []
    all_shortcodes: set = set()
    new_shortcodes: set = set()

    for (kind, name), items in sorted(sources.items()):
        found = set(items.keys())
        in_db = {sc for sc in found if sc in sc_to_id}
        new = found - in_db
        all_shortcodes |= found
        new_shortcodes |= new
        groups.append({
            'account': account,
            'kind': kind,
            'name': name,
            'found': len(found),
            'in_db': len(in_db),
            'new': len(new),
        })

    cost_usd = round(len(new_shortcodes) * _COST_PER_REEL_USD, 4)
    return {
        'account': account,
        'groups': groups,
        'total_unique': len(all_shortcodes),
        'total_new_unique': len(new_shortcodes),
        'new_shortcodes': sorted(new_shortcodes),
        'estimated_cost_usd': cost_usd,
        'estimated_cost_rub': round(cost_usd * _RUB_PER_USD, 2),
    }


def sync_to_db(sources: dict, account: str) -> dict:
    """Пишет метки в reel_sources; недостающие рилсы заводит через _setup_jobs (без перекачки)."""
    db = get_db()
    all_shortcodes = sorted({sc for items in sources.values() for sc in items})
    sc_to_id = _lookup_reel_ids(db, all_shortcodes)

    by_sc: dict = {}
    for items in sources.values():
        for sc, item in items.items():
            by_sc.setdefault(sc, item)

    missing = [sc for sc in all_shortcodes if sc not in sc_to_id]
    created = 0
    if missing:
        unique = [
            ParsedReel(shortcode=sc, type=by_sc[sc].type, url=by_sc[sc].url)
            for sc in missing
        ]
        req = _default_import_request(account)
        session_resp = db.table('import_sessions').insert({
            'source_type': req.source_type,
            'engine': req.engine,
            'model': req.model,
            'translate': req.translate,
            'pull_stats': req.pull_stats,
            'comment': req.comment,
            'total': len(unique),
            'status': 'running',
        }).execute()
        session_id = session_resp.data[0]['id']
        logger.info('Новых рилсов: %d — завожу через сессию %s', len(unique), session_id)
        _setup_jobs(session_id, unique, req)
        created = len(unique)
        sc_to_id.update(_lookup_reel_ids(db, missing))

    rows = []
    skipped = 0
    for (kind, name), items in sources.items():
        for sc, item in items.items():
            reel_id = sc_to_id.get(sc)
            if reel_id is None:
                logger.warning('%s: reel_id не найден даже после создания — пропускаю метку', sc)
                skipped += 1
                continue
            rows.append({
                'reel_id': reel_id,
                'account': account,
                'kind': kind,
                'name': name,
                'added_at': item.added_at,
            })

    upserted = 0
    for chunk in _chunks(rows, 500):
        db.table('reel_sources').upsert(chunk, on_conflict='reel_id,account,kind,name').execute()
        upserted += len(chunk)

    return {'new_reels_created': created, 'labels_upserted': upserted, 'labels_skipped': skipped}


def sync_labels_only(sources: dict, account: str) -> dict:
    """Проставляет метки ТОЛЬКО тем shortcode, что уже есть в `reels` — ничего не создаёт.

    Недостающие (которых ещё нет в базе) не трогает вообще: ни `reels`, ни `transcripts`, ни
    `jobs` — `_setup_jobs` здесь не вызывается ни разу. Их число просто возвращается, чтобы
    попасть в отчёт (владелец решает по нему, стоит ли запускать платный добор).
    """
    db = get_db()
    all_shortcodes = sorted({sc for items in sources.values() for sc in items})
    sc_to_id = _lookup_reel_ids(db, all_shortcodes)
    missing = [sc for sc in all_shortcodes if sc not in sc_to_id]

    rows = []
    for (kind, name), items in sources.items():
        for sc, item in items.items():
            reel_id = sc_to_id.get(sc)
            if reel_id is None:
                continue  # ещё не в базе — не создаём, добор делается отдельным явным прогоном
            rows.append({
                'reel_id': reel_id,
                'account': account,
                'kind': kind,
                'name': name,
                'added_at': item.added_at,
            })

    upserted = 0
    for chunk in _chunks(rows, 500):
        db.table('reel_sources').upsert(chunk, on_conflict='reel_id,account,kind,name').execute()
        upserted += len(chunk)

    return {'new_reels_created': 0, 'labels_upserted': upserted, 'labels_skipped': 0, 'not_in_db': len(missing)}


def _print_summary(report: dict) -> None:
    print(f"Учётка: {report['account']}")
    for g in report['groups']:
        print(f"  [{g['kind']}] {g['name']!r}: найдено {g['found']}, в базе {g['in_db']}, новых {g['new']}")
    print(f"Итог уникальных: {report['total_unique']}")
    print(f"Новых уникальных: {report['total_new_unique']}")
    print(f"Оценка стоимости добора: ${report['estimated_cost_usd']} (~{report['estimated_cost_rub']} ₽)")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    parser = argparse.ArgumentParser(
        description='Разметить рилсы метками происхождения (учётка + папка/директ) по выгрузке архива Instagram.',
    )
    parser.add_argument('--root', required=True, help='папка выгрузки одной учётки (там лежат saved/ и messages/)')
    parser.add_argument('--account', required=True, help='имя учётки для метки, напр. ilfaaat_sistema')
    parser.add_argument('--folders', default=None, help='список папок через запятую, либо "all"')
    parser.add_argument('--direct', action='store_true', help='взять ссылки из переписки (messages/**/*.json)')
    parser.add_argument('--dry-run', action='store_true', help='ничего не писать в БД, только посчитать и сохранить отчёт')
    parser.add_argument(
        '--labels-only', action='store_true',
        help='проставить метки только тем рилсам, что уже есть в базе; недостающие НЕ создавать '
             '(без трат — добор новых делается отдельным явным прогоном без этого флага)',
    )
    parser.add_argument('--out', default=None, help='путь для JSON-отчёта (обязателен по сути для --dry-run)')
    args = parser.parse_args()

    if args.folders is None and not args.direct:
        parser.error('нужно указать хотя бы одно: --folders или --direct')
    if args.dry_run and args.labels_only:
        parser.error('--dry-run и --labels-only взаимоисключающие — dry-run и так ничего не пишет')

    root = Path(args.root).expanduser()
    if not root.exists():
        parser.error(f'папка не найдена: {root}')

    sources = build_registry(root, args.folders, args.direct)
    if not sources:
        logger.warning('Ничего не найдено — проверь --root/--folders/--direct')
        return

    if args.dry_run:
        db = get_db()
        all_shortcodes = sorted({sc for items in sources.values() for sc in items})
        sc_to_id = _lookup_reel_ids(db, all_shortcodes)
        report = make_report(sources, args.account, sc_to_id)

        out_path = Path(args.out) if args.out else Path(f'/tmp/import_saved_{args.account}_dry_run.json')
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        logger.info('Отчёт сохранён: %s', out_path)

        _print_summary(report)
        return

    if args.labels_only:
        # Отчёт — ДО записи в БД: числа (сколько уже в базе / сколько новых осталось) не зависят
        # от факта апсерта меток, а посчитать и сохранить их первым делом — общее правило проекта
        # для любой не-бесплатной по времени операции.
        db = get_db()
        all_shortcodes = sorted({sc for items in sources.values() for sc in items})
        sc_to_id = _lookup_reel_ids(db, all_shortcodes)
        report = make_report(sources, args.account, sc_to_id)

        out_path = Path(args.out) if args.out else Path(f'/tmp/import_saved_{args.account}_labels_only.json')
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        logger.info('Отчёт сохранён: %s', out_path)

        result = sync_labels_only(sources, args.account)
        logger.info(
            'Готово (labels-only, без создания новых). Меток проставлено: %d, ещё не в базе '
            '(добор не запускался): %d',
            result['labels_upserted'], result['not_in_db'],
        )
        _print_summary(report)
        return

    result = sync_to_db(sources, args.account)
    logger.info(
        'Готово. Новых рилсов создано: %d, меток проставлено: %d, пропущено: %d',
        result['new_reels_created'], result['labels_upserted'], result['labels_skipped'],
    )

    if args.out:
        db = get_db()
        all_shortcodes = sorted({sc for items in sources.values() for sc in items})
        sc_to_id = _lookup_reel_ids(db, all_shortcodes)
        report = make_report(sources, args.account, sc_to_id)
        report['sync_result'] = result
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        logger.info('Отчёт сохранён: %s', out_path)


if __name__ == '__main__':
    main()
