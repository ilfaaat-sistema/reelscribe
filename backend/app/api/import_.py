from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse

from app.models.schemas import ImportRequest, ImportResponse
from app.services.import_service import handle_import

router = APIRouter(tags=["import"])


@router.post("/import", response_model=ImportResponse, status_code=status.HTTP_202_ACCEPTED)
async def import_links(body: ImportRequest) -> ImportResponse:
    if not body.links_text.strip():
        raise HTTPException(status_code=400, detail="links_text пустой")
    return await handle_import(body)
