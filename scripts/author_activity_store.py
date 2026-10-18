"""Атомарное хранение очереди и публичного отчёта в отдельной ветке данных GitHub."""
import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

REPOSITORY = 'ilfaaat-sistema/reelscribe'
BRANCH = 'data/authors-activity'
MARKER = {'kind': 'reelscribe-author-activity', 'version': 1}
FILES = ('checkpoint/selection.json', 'checkpoint/state.json', 'activity-report.json', 'metadata.json', 'vercel.json')


class StoreError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def validate_data(value):
    """Не допускать случайной публикации учётных данных в ветке с публичной статистикой."""
    forbidden = re.compile(r'token|secret|authorization|password|cookie', re.I)
    if isinstance(value, dict):
        for key, child in value.items():
            if key == 'authors' and isinstance(child, dict):
                for username, author in child.items():
                    if not re.fullmatch(r'[A-Za-z0-9._]{1,30}', username):
                        raise StoreError('Недопустимое имя автора в контрольной точке')
                    validate_data(author)
                continue
            if forbidden.search(key):
                raise StoreError('В данных обнаружено запрещённое поле авторизации')
            validate_data(child)
    elif isinstance(value, list):
        for child in value:
            validate_data(child)
    elif isinstance(value, str) and re.search(r'access_token=|Bearer\s+|gh[opsu]_[A-Za-z0-9]{30,}', value, re.I):
        raise StoreError('В данных обнаружен возможный секрет')


def github(method, path, payload=None, missing=False):
    token = os.environ.get('GH_TOKEN')
    if not token:
        raise StoreError('Не задан штатный токен GitHub Actions')
    request = Request('https://api.github.com/repos/' + REPOSITORY + '/' + path,
                      data=json.dumps(payload).encode() if payload is not None else None,
                      headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                               'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json'}, method=method)
    try:
        with urlopen(request, timeout=60) as response:
            if response.status == 204:
                return None
            return json.load(response)
    except HTTPError as error:
        if missing and error.code == 404:
            return None
        raise StoreError(f'GitHub вернул HTTP {error.code}; данные не перезаписаны', status_code=error.code) from None


def ref(api=github):
    return api('GET', 'git/ref/heads/' + quote(BRANCH, safe='/'), missing=True)


def read_blob(sha, api):
    blob = api('GET', 'git/blobs/' + sha)
    if blob.get('size', 0) > 30_000_000 or blob.get('encoding') != 'base64':
        raise StoreError('Неподдерживаемый размер или формат контрольной точки')
    value = json.loads(base64.b64decode(blob['content']))
    validate_data(value)
    return value


def tree_files(reference, api):
    commit = api('GET', 'git/commits/' + reference['object']['sha'])
    tree = api('GET', 'git/trees/' + commit['tree']['sha'] + '?recursive=1')
    if tree.get('truncated'):
        raise StoreError('Неполное дерево контрольной точки')
    blobs = {entry['path']: entry['sha'] for entry in tree['tree'] if entry['type'] == 'blob'}
    if set(blobs) != set(FILES) or read_blob(blobs['metadata.json'], api) != MARKER:
        raise StoreError('Ветка данных имеет другое назначение; публикация отменена')
    return blobs, commit['tree']['sha']


def load(directory, seed, api=github):
    directory.mkdir(parents=True, exist_ok=True)
    reference = ref(api)
    if reference is None:
        values = {name: json.loads((seed / name).read_text()) for name in ('selection.json', 'state.json')}
        base = None
    else:
        files, _ = tree_files(reference, api)
        values = {name: read_blob(files['checkpoint/' + name], api) for name in ('selection.json', 'state.json')}
        base = reference['object']['sha']
    for name, value in values.items():
        validate_data(value)
        (directory / name).write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    (directory / 'publication.json').write_text(json.dumps({'base_sha': base}), encoding='utf-8')
    return values['state.json']


def save(directory, report_path, api=github):
    publication = json.loads((directory / 'publication.json').read_text())
    reference = ref(api)
    current = reference['object']['sha'] if reference else None
    if current != publication['base_sha']:
        raise StoreError('Контрольная точка обновлена другим запуском; повторите чтение')
    if reference:
        tree_files(reference, api)
    selection = json.loads((directory / 'selection.json').read_text())
    state = json.loads((directory / 'state.json').read_text())
    report = json.loads(report_path.read_text())
    if report.get('provider') != 'meta_graph' or report.get('paid_runs') != 0:
        raise StoreError('Публикуется отчёт другого провайдера')
    if report['collection']['target'] != len(selection['authors']):
        raise StoreError('Число авторов не совпадает с контрольной точкой')
    values = {'checkpoint/selection.json': selection, 'checkpoint/state.json': state,
              'activity-report.json': report, 'metadata.json': MARKER,
              'vercel.json': {'git': {'deploymentEnabled': False}}}
    entries = []
    for path, value in values.items():
        validate_data(value)
        blob = api('POST', 'git/blobs', {'encoding': 'base64', 'content': base64.b64encode(json.dumps(value, ensure_ascii=False).encode()).decode()})
        entries.append({'path': path, 'mode': '100644', 'type': 'blob', 'sha': blob['sha']})
    tree = api('POST', 'git/trees', {'tree': entries})
    commit = api('POST', 'git/commits', {'message': 'Обновление бесплатного сбора активности авторов',
                                        'tree': tree['sha'], 'parents': [current] if current else []})
    if current:
        api('PATCH', 'git/refs/heads/' + BRANCH, {'sha': commit['sha'], 'force': False})
    else:
        api('POST', 'git/refs', {'ref': 'refs/heads/' + BRANCH, 'sha': commit['sha']})
    (directory / 'publication.json').write_text(json.dumps({'base_sha': commit['sha']}), encoding='utf-8')
    return commit['sha']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('load', 'save'))
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--seed', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    try:
        if args.action == 'load':
            load(args.directory, args.seed)
            print('Контрольная точка загружена')
        else:
            save(args.directory, args.report)
            print('Очередь и отчёт опубликованы атомарно')
    except Exception as error:
        print(str(error) if isinstance(error, StoreError) else f'Ошибка хранения: {type(error).__name__}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
