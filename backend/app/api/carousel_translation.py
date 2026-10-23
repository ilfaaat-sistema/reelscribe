"""Явный перевод сохранённой подписи без произвольного исходника клиента."""
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.services import carousel_translation as service

router = APIRouter(prefix='/radar/carousels', tags=['carousels'])


class TranslateCarousel(BaseModel):
    model_config = ConfigDict(extra='forbid')
    period: Literal['7d', '14d', '30d', 'archive14d'] | None = None
    source_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    automatic: bool = Field(default=False, strict=True)


@router.post('/{media_id}/translate')
async def translate(media_id: str, body: TranslateCarousel, request: Request):
    headers = {'Cache-Control': 'no-store'}
    try:
        result = await service.translate(media_id, body.period, body.source_hash,
                                         oidc_token=request.headers.get('x-vercel-oidc-token'),
                                         automatic=body.automatic)
    except service.TranslationError as exc:
        if exc.retry_after:
            headers['Retry-After'] = str(exc.retry_after)
        return JSONResponse({'detail': exc.detail, 'code': exc.code}, status_code=exc.status, headers=headers)
    return JSONResponse(result, headers=headers)
