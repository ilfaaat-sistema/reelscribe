import httpx
import pytest
from io import BytesIO
from PIL import Image, ImageDraw
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


def encoded(source, format='PNG', **kwargs):
    output = BytesIO()
    source.save(output, format=format, **kwargs)
    return output.getvalue()


def test_normalization_rgb_baseline_strips_metadata_and_keeps_full_frame():
    source = Image.new('RGB', (3000, 1500), 'white')
    drawing = ImageDraw.Draw(source)
    drawing.rectangle((0, 0, 400, 400), fill='red')
    drawing.rectangle((2600, 1100, 2999, 1499), fill='blue')
    exif = Image.Exif()
    exif[270] = 'Служебная подпись'
    data = encoded(source, 'JPEG', progressive=True, exif=exif,
                   icc_profile=b'test-profile', comment=b'test-comment')
    normalized, mime = image.normalize_image(data, 'image/jpeg')
    with Image.open(BytesIO(normalized)) as result:
        assert mime == 'image/jpeg' and result.format == 'JPEG'
        assert result.mode == 'RGB' and result.size == (2048, 1024)
        assert not result.getexif()
        assert not any(key in result.info for key in ('exif', 'icc_profile', 'comment', 'progressive', 'progression'))
        red = result.getpixel((10, 10))
        blue = result.getpixel((2037, 1013))
        assert red[0] > 240 and red[2] < 10
        assert blue[2] > 240 and blue[0] < 10


@pytest.mark.parametrize('mode,size,format,mime', [
    ('RGBA', (400, 200), 'PNG', 'image/png'),
    ('L', (200, 400), 'PNG', 'image/png'),
    ('RGB', (1000, 3000), 'WEBP', 'image/webp'),
])
def test_normalization_keeps_aspect_ratio_and_never_upscales(mode, size, format, mime):
    source = Image.new(mode, size)
    data, _ = image.normalize_image(encoded(source, format), mime)
    with Image.open(BytesIO(data)) as result:
        assert result.mode == 'RGB'
        scale = min(1, 2048 / max(size))
        assert all(abs(actual - original * scale) <= 1 for actual, original in zip(result.size, size))
        if mode == 'RGBA':
            assert all(channel > 250 for channel in result.getpixel((10, 10)))


def test_normalization_applies_display_orientation_before_removing_exif():
    source = Image.new('RGB', (80, 40), 'white')
    ImageDraw.Draw(source).rectangle((0, 0, 39, 39), fill='red')
    exif = Image.Exif()
    exif[274] = 6
    data, _ = image.normalize_image(encoded(source, 'JPEG', exif=exif), 'image/jpeg')
    with Image.open(BytesIO(data)) as result:
        assert result.size == (40, 80) and not result.getexif()
        assert result.getpixel((20, 10))[0] > 240
        assert result.getpixel((20, 10))[1] < 10
        assert min(result.getpixel((20, 70))) > 240


@pytest.mark.parametrize('data,mime', [(b'\xff\xd8\xffbroken', 'image/jpeg'),
    (b'not-image', 'image/png'), (b'\x89PNG\r\n\x1a\n', 'text/html')])
def test_normalization_rejects_corrupt_or_mismatched_image(data, mime):
    with pytest.raises(image.ImageUnavailable):
        image.normalize_image(data, mime)


def test_normalization_bounds_decoded_pixels_before_loading(monkeypatch):
    data = encoded(Image.new('RGB', (20, 20)))
    monkeypatch.setattr(image, 'MAX_NORMALIZATION_PIXELS', 100)
    with pytest.raises(image.ImageUnavailable):
        image.normalize_image(data, 'image/png')


@pytest.mark.parametrize('cover', [{'shortcode': '../bad'}, {'shortcode': 'a?url=x'},
    {'permalink': 'https://evil.test/p/Abc123/'}, {'permalink': 'https://instagram.com/p/Abc123/../../a'}])
def test_public_source_rejects_untrusted_shortcode(cover):
    assert image.public_cover_url(cover) is None


@pytest.mark.asyncio
async def test_expired_signature_and_timeout_are_distinct():
    with pytest.raises(image.ImageUnavailable) as expired:
        await image.download_image('https://x.fbcdn.net/a?oe=1', client_factory=lambda **kwargs:
            httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(403)), **kwargs))
    assert expired.value.reason_code == 'expired_signature'
    def timeout(request):
        raise httpx.ReadTimeout('private signed URL')
    with pytest.raises(image.ImageUnavailable) as timed:
        await fetch(timeout)
    assert timed.value.reason_code == 'timeout' and 'private' not in str(timed.value)
