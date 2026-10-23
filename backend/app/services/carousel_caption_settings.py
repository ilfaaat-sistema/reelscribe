"""Изолированная конфигурация caption: платный запуск по умолчанию закрыт."""
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class CaptionSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix='CAROUSEL_CAPTION_', extra='ignore')

    translation_enabled: bool = False
    vertex_project: str = ''
    vertex_location: str = 'global'
    vertex_model: str = ''
    auth_mode: Literal['vercel_oidc'] = 'vercel_oidc'
    wif_audience: str = ''
    service_account_email: str = ''
    oidc_issuer: str = ''
    oidc_audience: str = ''
    oidc_owner_id: str = ''
    oidc_project_id: str = ''
    oidc_team: str = ''
    oidc_project_name: str = ''

    @property
    def enabled(self):
        return bool(self.translation_enabled
                    and self.vertex_location == 'global'
                    and self.vertex_model == 'gemini-3.1-flash-lite'
                    and all((self.vertex_project, self.wif_audience, self.service_account_email,
                             self.oidc_issuer, self.oidc_audience, self.oidc_owner_id,
                             self.oidc_project_id, self.oidc_team, self.oidc_project_name)))


@lru_cache
def get_settings():
    return CaptionSettings()
