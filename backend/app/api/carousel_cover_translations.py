"""Пакетное чтение кэша и явный запуск перевода существующей обложки."""
import re
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from app.services import carousel_cover_service as service

router = APIRouter(prefix='/radar/carousel-cover-translations', tags=['carousels'])


class CoverRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    period: Literal['7d', '14d', '30d', 'archive14d']


def validate_id(value):
    if not re.fullmatch(r'[0-9]{1,40}', value):
        raise HTTPException(422, 'Некорректный ID карусели')
    return value


@router.get('')
async def read_covers(response: Response, ids: str = Query(..., max_length=4100)):
    response.headers['Cache-Control'] = 'no-store'
    values = ids.split(',')
    if not 1 <= len(values) <= 100:
        raise HTTPException(422, 'Допустимо от 1 до 100 ID')
    return await service.read(list(dict.fromkeys(validate_id(value) for value in values)))


@router.post('/{media_id}')
async def translate_cover(media_id: str, body: CoverRequest, response: Response, request: Request):
    response.headers['Cache-Control'] = 'no-store'
    return await service.translate(validate_id(media_id), body.period,
                                   oidc_token=request.headers.get('x-vercel-oidc-token'))
