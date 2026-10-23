"""Изолированная авторизация обложек: ADC локально, короткие OIDC credentials на Vercel."""
import base64
import json
import re
import time

from google.auth import identity_pool
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
import requests

from app.services.carousel_cover_settings import get_settings

SCOPES = ['https://www.googleapis.com/auth/cloud-platform']


class CoverAuthError(RuntimeError):
    """Безопасная ошибка без содержимого токена или ответа провайдера."""


def _part(value):
    return json.loads(base64.urlsafe_b64decode(value + '=' * (-len(value) % 4)))


def validate_claims(token, config, now=None):
    """Предварительные ограничения; подпись обязательно проверяет Google STS при refresh."""
    if not isinstance(token, str) or not 1 <= len(token) <= 16384:
        raise CoverAuthError('Серверная авторизация обложек недоступна')
    try:
        header, payload, signature = token.split('.')
        head, claims = _part(header), _part(payload)
        clock = time.time() if now is None else now
        subject = f'owner:{config.oidc_team}:project:{config.oidc_project_name}:environment:'
        expiry, issued = claims.get('exp'), claims.get('iat')
        valid = (
            isinstance(head, dict) and head.get('alg') == 'RS256' and bool(head.get('kid'))
            and bool(signature) and isinstance(claims, dict)
            and config.oidc_issuer and claims.get('iss') == config.oidc_issuer
            and config.oidc_audience and claims.get('aud') == config.oidc_audience
            and config.oidc_owner_id and claims.get('owner_id') == config.oidc_owner_id
            and config.oidc_project_id and claims.get('project_id') == config.oidc_project_id
            and claims.get('owner') == config.oidc_team
            and claims.get('project') == config.oidc_project_name
            and claims.get('environment') in ('preview', 'production')
            and claims.get('sub') == subject + claims.get('environment', '')
            and type(expiry) is int and clock < expiry
            and type(issued) is int and issued <= clock + 30
            and type(claims.get('nbf', issued)) is int and claims.get('nbf', issued) <= clock + 30
        )
        if not valid:
            raise ValueError('invalid_claims')
    except Exception:
        raise CoverAuthError('Серверная авторизация обложек недоступна') from None
    return token


class RequestTokenSupplier(identity_pool.SubjectTokenSupplier):
    """Поставщик принадлежит одному HTTP-запросу и не кэшируется между пользователями."""

    def __init__(self, token, config):
        self._token = token
        self._config = config

    def get_subject_token(self, context, request):
        try:
            return validate_claims(self._token, self._config)
        except CoverAuthError:
            raise RefreshError('Серверная авторизация обложек недоступна') from None


def oidc_credentials(token, *, config=None, refresh=True):
    config = config or get_settings()
    if (not re.fullmatch(r'//iam\.googleapis\.com/projects/[0-9]+/locations/global/workloadIdentityPools/[a-z0-9-]+/providers/[a-z0-9-]+', config.wif_audience)
            or not re.fullmatch(r'[a-z0-9-]+@' + re.escape(config.vertex_project) + r'\.iam\.gserviceaccount\.com', config.service_account_email)):
        raise CoverAuthError('Серверная авторизация обложек не настроена')
    validate_claims(token, config)
    credentials = identity_pool.Credentials(
        audience=config.wif_audience,
        subject_token_type='urn:ietf:params:oauth:token-type:jwt',
        token_url='https://sts.googleapis.com/v1/token',
        subject_token_supplier=RequestTokenSupplier(token, config),
        service_account_impersonation_url=(
            'https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/'
            + config.service_account_email + ':generateAccessToken'),
        scopes=SCOPES,
    )
    if refresh:
        # STS проверяет подпись и IAM-права ДО резервирования платного вызова.
        try:
            with requests.Session() as session:
                session.trust_env = False
                transport = Request(session=session)

                def bounded_request(*args, **kwargs):
                    kwargs['timeout'] = 10
                    return transport(*args, **kwargs)

                credentials.refresh(bounded_request)
        except Exception:
            raise CoverAuthError('Не удалось подтвердить серверную авторизацию обложек') from None
    return credentials
