import httpx
import pytest
from app.services import carousel_cover_image as image


@pytest.mark.parametrize('url', ['http://x.fbcdn.net/a', 'https://fbcdn.net.evil.test/a',
    'https://127.0.0.1/a', 'https://x.fbcdn.net:8443/a', 'https://user@x.fbcdn.net/a',
    'https://x.fbcdn.net/a#x', None, 'file:///etc/passwd'])
def test_rejects_unsafe_url(url):
    with pytest.raises(image.ImageUnavailable):
        image.validate_url(url)


async def fetch(handler):
    return await image.download_image('https://x.fbcdn.net/a',
        client_factory=lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs))


@pytest.mark.asyncio
async def test_download_full_image_and_allowlisted_redirect():
    seen = []
    def handler(request):
        seen.append(str(request.url))
        if request.url.path == '/a':
            return httpx.Response(302, headers={'location': 'https://y.cdninstagram.com/b'})
        return httpx.Response(200, headers={'content-type': 'image/jpeg'}, content=b'\xff\xd8\xffFULL-LOWER-HEADLINE')
    data, mime = await fetch(handler)
    assert data.endswith(b'LOWER-HEADLINE') and mime == 'image/jpeg'
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_redirect_to_private_host_never_requested():
    seen = []
    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={'location': 'https://127.0.0.1/secret'})
    with pytest.raises(image.ImageUnavailable):
        await fetch(handler)
    assert len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('headers,content', [({'content-type': 'text/html'}, b'html'),
    ({'content-type': 'image/jpeg'}, b'html'),
    ({'content-type': 'image/jpeg', 'content-length': '999999999'}, b'\xff\xd8\xff')])
async def test_rejects_wrong_mime_signature_and_declared_size(headers, content):
    with pytest.raises(image.ImageUnavailable):
        await fetch(lambda request: httpx.Response(200, headers=headers, content=content))


@pytest.mark.asyncio
async def test_actual_size_limit_without_trusting_header(monkeypatch):
    monkeypatch.setattr(image, 'MAX_IMAGE_BYTES', 10)
    with pytest.raises(image.ImageUnavailable):
        await fetch(lambda request: httpx.Response(200, headers={'content-type': 'image/jpeg'}, content=b'\xff\xd8\xff' + b'x' * 20))


@pytest.mark.asyncio
async def test_redirect_loop_is_bounded():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={'location': '/a'})
    with pytest.raises(image.ImageUnavailable):
        await fetch(handler)
    assert len(calls) == 4
