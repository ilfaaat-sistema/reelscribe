"""Общий список скрытых авторов; записи меняются только через узкие RPC."""
import asyncio
import re

from fastapi import HTTPException

from app.core.db import get_db

COLUMNS = 'username,hidden_at'


def validate_username(username):
    if not isinstance(username, str) or not re.fullmatch(r'[a-z0-9._]{1,30}', username):
        raise HTTPException(422, 'Некорректное имя автора')
    return username


def _item(row):
    return {'username': validate_username(row['username']), 'hidden_at': row['hidden_at']}


def _read_all():
    authors = []
    start = 0
    while True:
        rows = (get_db().table('carousel_hidden_authors').select(COLUMNS).eq('active', True)
                .order('hidden_at', desc=True).order('username').range(start, start + 999)
                .execute().data or [])
        authors.extend(_item(row) for row in rows)
        if len(rows) < 1000:
            return {'authors': authors, 'total': len(authors)}
        start += 1000


def _set(username, hidden):
    data = get_db().rpc('set_carousel_author_hidden', {
        'p_username': username, 'p_hidden': hidden}).execute().data
    if isinstance(data, list) and len(data) == 1:
        data = data[0]
    if not isinstance(data, dict) or data.get('username') != username:
        raise ValueError('Некорректный ответ записи')
    if hidden:
        return {'item': _item(data)}
    if data.get('hidden') is not False:
        raise ValueError('Некорректный ответ возврата')
    return {'username': username, 'hidden': False}


def _import(authors):
    data = get_db().rpc('import_carousel_hidden_authors', {'p_authors': authors}).execute().data
    if not isinstance(data, dict) or not isinstance(data.get('authors'), list):
        raise ValueError('Некорректный ответ импорта')
    result = [_item(row) for row in data['authors']]
    return {'authors': result, 'total': len(result)}


async def _database(fn, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except Exception:
        raise HTTPException(503, 'Не удалось прочитать или сохранить скрытых авторов. Попробуйте позже.') from None


async def list_authors():
    return await _database(_read_all)


async def set_hidden(username, hidden):
    validate_username(username)
    return await _database(_set, username, hidden)


async def import_authors(authors):
    return await _database(_import, authors)
