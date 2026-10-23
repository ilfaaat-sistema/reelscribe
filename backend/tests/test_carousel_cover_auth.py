"""OIDC без сети: границы проекта, короткие credentials и безопасные ошибки STS."""
import base64
import json
import time
from urllib.parse import parse_qs

import pytest
import requests
from google.auth.exceptions import RefreshError

from app.services import carousel_cover_auth as auth
from app.services.carousel_cover_settings import CoverSettings


def config():
    return CoverSettings(
        vertex_project='test-project', auth_mode='vercel_oidc',
        wif_audience='//iam.googleapis.com/projects/123/locations/global/workloadIdentityPools/test/providers/vercel',
        service_account_email='cover@test-project.iam.gserviceaccount.com',
        oidc_issuer='https://oidc.vercel.com/test-team', oidc_audience='https://vercel.com/test-team',
        oidc_owner_id='team_123', oidc_project_id='prj_123', oidc_team='test-team',
    )


def token(**changes):
    now = int(time.time())
    claims = {'iss': config().oidc_issuer, 'aud': config().oidc_audience,
              'owner_id': 'team_123', 'project_id': 'prj_123', 'owner': 'test-team',
              'project': 'reelscribe', 'environment': 'preview',
              'sub': 'owner:test-team:project:reelscribe:environment:preview',
              'iat': now - 1, 'nbf': now - 1, 'exp': now + 3600}
    claims.update(changes)
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')
    return encode({'alg': 'RS256', 'kid': 'fake-kid'}) + '.' + encode(claims) + '.fake-signature'


@pytest.mark.parametrize('changes', [
    {'iss': 'https://attacker.test'}, {'aud': 'wrong'}, {'owner_id': 'team_other'},
    {'project_id': 'prj_other'}, {'environment': 'development'}, {'sub': 'wrong'},
    {'exp': 1}, {'exp': True}, {'nbf': 9999999999}, {'iat': 9999999999},
])
def test_wrong_claims_rejected_before_transport(changes):
    with pytest.raises(auth.CoverAuthError):
        auth.validate_claims(token(**changes), config())


def test_supplier_revalidates_expiry_without_caching(monkeypatch):
    signed = token()
    supplier = auth.RequestTokenSupplier(signed, config())
    assert supplier.get_subject_token(None, None) == signed
    monkeypatch.setattr(auth.time, 'time', lambda: time_value)
    time_value = 9999999999
    with pytest.raises(RefreshError):
        supplier.get_subject_token(None, None)


def test_adc_other_project_cannot_be_used_for_oidc_account():
    settings = config().model_copy(update={'service_account_email': 'cover@another-project.iam.gserviceaccount.com'})
    with pytest.raises(auth.CoverAuthError):
        auth.oidc_credentials(token(), config=settings, refresh=False)


def test_sts_signature_check_and_impersonation_complete_before_model(monkeypatch):
    seen = []

    def send(adapter, request, **kwargs):
        seen.append(request)
        assert kwargs['timeout'] == 10
        response = requests.Response()
        response.status_code = 200
        if request.url == 'https://sts.googleapis.com/v1/token':
            form = parse_qs(request.body.decode() if isinstance(request.body, bytes) else request.body)
            assert form['audience'] == [config().wif_audience]
            body = {'access_token': 'fake-sts-access', 'expires_in': 3600,
                    'token_type': 'Bearer', 'issued_token_type': 'urn:ietf:params:oauth:token-type:access_token'}
        else:
            assert request.url.startswith('https://iamcredentials.googleapis.com/v1/')
            body = {'accessToken': 'fake-impersonated-access', 'expireTime': '2099-01-01T00:00:00Z'}
        response._content = json.dumps(body).encode()
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', send)
    credentials = auth.oidc_credentials(token(), config=config())
    assert credentials.token == 'fake-impersonated-access'
    assert len(seen) == 2


def test_sts_rejection_is_safe_and_no_api_key_fallback(monkeypatch):
    seen = []

    def send(adapter, request, **kwargs):
        seen.append(request.url)
        response = requests.Response()
        response.status_code = 400
        response._content = b'{"error":"invalid_grant","error_description":"private-provider-detail"}'
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', send)
    with pytest.raises(auth.CoverAuthError) as caught:
        auth.oidc_credentials(token(), config=config())
    assert 'private-provider-detail' not in str(caught.value)
    assert seen == ['https://sts.googleapis.com/v1/token']
