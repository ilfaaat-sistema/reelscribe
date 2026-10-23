"""Загрузка полного изображения с проверкой каждого адреса и ограничением памяти."""
import asyncio
from io import BytesIO
from urllib.parse import urljoin, urlsplit

import httpx

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_NORMALIZATION_PIXELS = 25_000_000
ALLOWED_HOSTS = ('cdninstagram.com', 'fbcdn.net', 'instagram.com')


class ImageUnavailable(Exception):
    pass


def validate_url(value):
    try:
        url = urlsplit(value)
        host = (url.hostname or '').lower()
        if (url.scheme != 'https' or url.username or url.password or url.port not in (None, 443)
                or url.fragment or not any(host == h or host.endswith('.' + h) for h in ALLOWED_HOSTS)):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ImageUnavailable('Изображение недоступно') from None
    return value


def valid_signature(data, mime):
    return bool((mime == 'image/jpeg' and data.startswith(b'\xff\xd8\xff'))
                or (mime == 'image/png' and data.startswith(b'\x89PNG\r\n\x1a\n'))
                or (mime == 'image/webp' and data.startswith(b'RIFF') and data[8:12] == b'WEBP'))


def normalize_image(data, mime):
    """Контролируемый вариант: полный кадр, RGB JPEG без metadata, длинная сторона до 2048."""
    from PIL import Image, ImageOps

    try:
        if len(data) > MAX_IMAGE_BYTES or not valid_signature(data, mime):
            raise ImageUnavailable()
        with Image.open(BytesIO(data)) as source:
            if source.width * source.height > MAX_NORMALIZATION_PIXELS:
                raise ImageUnavailable()
            # Ориентация сохраняется визуально до удаления EXIF; кадр не обрезается.
            oriented = ImageOps.exif_transpose(source)
            oriented.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
            rgba = oriented.convert('RGBA')
            rgb = Image.new('RGB', rgba.size, 'white')
            rgb.paste(rgba, mask=rgba.getchannel('A'))
            output = BytesIO()
            rgb.save(output, format='JPEG', quality=95, subsampling=0,
                     progressive=False, optimize=False)
        result = output.getvalue()
        if len(result) > MAX_IMAGE_BYTES:
            raise ImageUnavailable()
        return result, 'image/jpeg'
    except (OSError, ValueError, Image.DecompressionBombError, ImageUnavailable):
        raise ImageUnavailable('Изображение недоступно') from None


async def download_image(url, client_factory=httpx.AsyncClient):
    try:
        async with asyncio.timeout(20):
            async with client_factory(timeout=10, follow_redirects=False, trust_env=False,
                                      headers={'Accept-Encoding': 'identity'}) as client:
                for _ in range(4):
                    validate_url(url)
                    async with client.stream('GET', url) as response:
                        if response.status_code in (301, 302, 303, 307, 308):
                            location = response.headers.get('location')
                            if not location:
                                raise ImageUnavailable()
                            url = validate_url(urljoin(url, location))
                            continue
                        response.raise_for_status()
                        mime = response.headers.get('content-type', '').split(';')[0].strip().lower()
                        if mime not in ('image/jpeg', 'image/png', 'image/webp'):
                            raise ImageUnavailable()
                        length = response.headers.get('content-length')
                        if length and (not length.isdigit() or int(length) > MAX_IMAGE_BYTES):
                            raise ImageUnavailable()
                        content = bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=65536):
                            content.extend(chunk)
                            if len(content) > MAX_IMAGE_BYTES:
                                raise ImageUnavailable()
                        if not valid_signature(content, mime):
                            raise ImageUnavailable()
                        return bytes(content), mime
                raise ImageUnavailable()
    except (httpx.HTTPError, TimeoutError, ValueError, ImageUnavailable):
        raise ImageUnavailable('Изображение недоступно') from None
