"""Публичный общий список скрытых авторов без входа."""
from datetime import datetime
import re

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from app.services import hidden_authors

router = APIRouter(prefix='/radar/hidden-authors', tags=['hidden-authors'])


class ImportedAuthor(BaseModel):
    model_config = ConfigDict(extra='forbid')
    username: StrictStr
    hidden_at: StrictStr

    @field_validator('username')
    @classmethod
    def username_valid(cls, value):
        if not re.fullmatch(r'[a-z0-9._]{1,30}', value):
            raise ValueError('Некорректное имя автора')
        return value

    @field_validator('hidden_at')
    @classmethod
    def date_valid(cls, value):
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})', value):
            raise ValueError('Нужна дата ISO с часовым поясом')
        datetime.fromisoformat(value.replace('Z', '+00:00'))
        return value


class ImportAuthors(BaseModel):
    model_config = ConfigDict(extra='forbid')
    authors: list[ImportedAuthor] = Field(max_length=1600)


def _response(value):
    return JSONResponse(value, headers={'Cache-Control': 'no-store'})


@router.get('')
async def list_authors():
    return _response(await hidden_authors.list_authors())


@router.post('/import')
async def import_authors(body: ImportAuthors):
    return _response(await hidden_authors.import_authors([item.model_dump() for item in body.authors]))


async def _empty_body(request):
    if await request.body():
        raise HTTPException(422, 'Тело запроса не предусмотрено')


@router.put('/{username}')
async def hide(username: str, request: Request):
    await _empty_body(request)
    return _response(await hidden_authors.set_hidden(username, True))


@router.delete('/{username}')
async def restore(username: str, request: Request):
    await _empty_body(request)
    return _response(await hidden_authors.set_hidden(username, False))
