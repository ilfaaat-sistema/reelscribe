"""Публичный общий список отбора каруселей без входа."""
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.services import carousel_likes

router = APIRouter(prefix='/radar/carousel-likes', tags=['carousel-likes'])


class SelectCarousel(BaseModel):
    model_config = ConfigDict(extra='forbid')
    period: Literal['7d', '14d', '30d', '4m', 'custom', 'archive14d']


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


class SaveNote(BaseModel):
    model_config = ConfigDict(extra='forbid')
    note: str = Field(strict=True, max_length=3000)
    expected_revision: int = Field(strict=True, ge=0, le=2147483646)

    @field_validator('note', mode='before')
    @classmethod
    def valid_text(cls, value):
        if isinstance(value, str) and ('\x00' in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value)):
            raise HTTPException(422, 'Некорректный символ в заметке')
        return value


@router.patch('/{media_id}/note')
async def save_note(media_id: str, body: SaveNote):
    return _response(await carousel_likes.save_note(media_id, body.note, body.expected_revision))
