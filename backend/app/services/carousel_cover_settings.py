"""Отдельная конфигурация перевода обложек; по умолчанию платный запуск закрыт."""
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class CoverSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix='CAROUSEL_COVER_', extra='ignore')

    translation_enabled: bool = False
    vertex_project: str = ''
    vertex_location: str = 'global'
    vertex_model: str = ''
    # Включать только для отдельной проверки после подтверждённого сбоя исходного изображения.
    image_normalization: Literal['none', 'jpeg2048'] = 'none'
    auth_mode: Literal['adc', 'vercel_oidc'] = 'adc'
    wif_audience: str = ''
    service_account_email: str = ''
    oidc_issuer: str = ''
    oidc_audience: str = ''
    oidc_owner_id: str = ''
    oidc_project_id: str = ''
    oidc_team: str = ''
    oidc_project_name: str = 'reelscribe'

    @property
    def enabled(self):
        return bool(self.translation_enabled and self.vertex_project and self.vertex_model)


@lru_cache
def get_settings():
    return CoverSettings()
