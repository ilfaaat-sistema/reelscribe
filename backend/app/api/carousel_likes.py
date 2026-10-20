"""Публичный общий список отбора каруселей без входа."""
from typing import Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from app.services import carousel_likes

router = APIRouter(prefix='/radar/carousel-likes', tags=['carousel-likes'])


class SelectCarousel(BaseModel):
    model_config = ConfigDict(extra='forbid')
    period: Literal['7d', '14d', '30d', 'archive14d']


def _response(value):
    return JSONResponse(value, headers={'Cache-Control': 'no-store'})


@router.get('')
async def list_likes():
    return _response(await carousel_likes.list_likes())


@router.put('/{media_id}')
async def select(media_id: str, body: SelectCarousel):
    return _response(await carousel_likes.select(media_id, body.period))


@router.delete('/{media_id}')
async def deselect(media_id: str):
    return _response(await carousel_likes.deselect(media_id))
