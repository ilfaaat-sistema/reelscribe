"""Личная подборка каруселей: проверка входа и границ HTTP-контракта."""
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from app.core.user_auth import get_current_user_id
from app.api.carousels import _report
from app.services import carousel_selections as service


class PrivateRoute(APIRoute):
    """Запрещает публичное кэширование также ошибок входа и валидации."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def private_handler(request: Request):
            try:
                response = await handler(request)
            except service.CarouselSelectionError as error:
                response = JSONResponse({'detail': error.detail}, status_code=error.status_code)
            except HTTPException as error:
                response = await http_exception_handler(request, error)
            except RequestValidationError as error:
                response = await request_validation_exception_handler(request, error)
            response.headers['Cache-Control'] = 'private, no-store'
            return response

        return private_handler


router = APIRouter(prefix='/radar/carousel-selections', tags=['carousel-selections'], route_class=PrivateRoute)
MediaId = Annotated[str, Field(pattern=r'^[0-9]{1,40}$')]
UserId = Annotated[str, Depends(get_current_user_id)]


class StatusBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    ids: list[MediaId] = Field(max_length=100)


class SelectBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_period: Literal['7d', '14d']
    expected_revision: Annotated[StrictInt, Field(ge=0)]


class AvailabilityBody(StatusBody):
    period: Literal['7d', '14d'] = '14d'


@router.get('')
@router.get('/', include_in_schema=False)
def list_selections(user_id: UserId, q: str = Query('', max_length=200),
                    sort: Literal['selected_at', 'timestamp', 'likes', 'comments'] = 'selected_at',
                    order: Literal['asc', 'desc'] = 'desc', limit: int = 50,
                    offset: int = Query(0, ge=0)):
    if limit not in (50, 100):
        raise HTTPException(422, 'Размер страницы должен быть 50 или 100')
    return service.list_selections(user_id, q, sort, order, limit, offset)


@router.post('/status')
def selection_status(body: StatusBody, user_id: UserId):
    return service.selection_status(user_id, body.ids)


@router.post('/availability')
async def selection_availability(body: AvailabilityBody, user_id: UserId):
    try:
        report = await _report(body.period)
    except HTTPException as error:
        if error.status_code == 503:
            return {'status': 'unknown', 'states': []}
        raise
    requested = set(body.ids)
    present = {str(item.get('id')) for item in report.get('items', [])
               if isinstance(item, dict) and item.get('media_type') == 'CAROUSEL_ALBUM'
               and str(item.get('id')) in requested}
    return {'status': 'known', 'states': [{'id': media_id, 'in_report': media_id in present}
                                        for media_id in dict.fromkeys(body.ids)]}


@router.get('/{id}')
def get_selection(user_id: UserId, id: str = Path(pattern=r'^[0-9]{1,40}$')):
    return service.get_selection(user_id, id)


@router.put('/{id}')
def select_carousel(body: SelectBody, user_id: UserId, id: str = Path(pattern=r'^[0-9]{1,40}$')):
    return service.select_carousel(user_id, id, body.source_period, body.expected_revision)


@router.delete('/{id}')
def remove_selection(user_id: UserId, id: str = Path(pattern=r'^[0-9]{1,40}$'),
                     expected_revision: int = Query(..., ge=0)):
    return service.remove_selection(user_id, id, expected_revision)
