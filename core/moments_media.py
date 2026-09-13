"""On-demand Moments image reading for comment generation.

Image download URLs and their keys live only in the encrypted sns.db timeline XML.
They are read transiently here to fetch and describe a picture, and are NEVER
persisted into the feed cache or returned to any client (see moments.parse_feed,
which deliberately drops them). Nothing here runs at sync/parse time: download and
the vision model are touched only while a reply to a picture post is being drafted.
"""
import io
import ipaddress
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from core import account_session as sessions, moments as m, llm
from core.media_read import VISION_PROMPT

MAX_IMAGE_BYTES = m.MAX_IMAGE_BYTES
_TIMEOUT = 20
_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')
# Tencent image/moments CDN only; anything else (private hosts, IP literals,
# the test's 127.0.0.1) is refused to avoid SSRF via attacker-controlled feed XML.
_ALLOWED_HOST = re.compile(r'(?:^|\.)(?:qpic\.cn|qlogo\.cn|weixin\.qq\.com)$', re.I)
_IMAGE_MEDIA_TYPES = {'2'}  # SNS media type 2 = image (type 6 = video)

_desc_cache = {}
_cache_lock = threading.Lock()
_CACHE_MAX = 512


def _host_ok(url):
    try:
        parts = urllib.parse.urlsplit(url)
    except (ValueError, AttributeError):
        return False
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        return False
    host = parts.hostname
    try:
        ipaddress.ip_address(host)
        return False  # never talk to a raw IP
    except ValueError:
        pass
    return bool(_ALLOWED_HOST.search(host))


class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _host_ok(newurl):
            raise urllib.error.HTTPError(newurl, code, 'blocked redirect host', headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_GuardedRedirect())


def image_sources(xml_str):
    """Extract image download descriptors from raw timeline XML. Internal only."""
    out = []
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return out
    t = root.find('TimelineObject')
    if t is None:
        return out
    for media in t.findall('./ContentObject/mediaList/media')[:20]:
        if (media.findtext('type') or '').strip() not in _IMAGE_MEDIA_TYPES:
            continue
        mid = (media.findtext('id') or '').strip()[:100]
        # Prefer the full image; fall back to the thumbnail for a cheaper read.
        for tag in ('url', 'thumb'):
            node = media.find(tag)
            if node is None or not (node.text or '').strip():
                continue
            url = node.text.strip()
            if not _host_ok(url):
                continue
            # Encrypted feeds carry a per-media token/enc_idx; without the token the
            # CDN answers 400, so they must ride along on the download request.
            out.append(dict(id=mid or url, url=url,
                            key=(node.get('key') or '').strip(),
                            token=(node.get('token') or '').strip(),
                            idx=(node.get('enc_idx') or '').strip()))
            break
    return out


def _valid_image(data):
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            mime = Image.MIME.get(im.format)
        if mime in ('image/jpeg', 'image/png', 'image/gif', 'image/webp'):
            return mime
    except Exception:
        pass
    return None


def _sns_decrypt(data, source):
    """Decrypt an encrypted SNS payload (x-Enc:1) → (bytes, mime) or (None, None).

    Encrypted moments media are a keystream XOR (NOT AES), per kanxue "微信4.0朋友圈
    媒体解密全解析". The keystream comes from WeChat's own `WxIsaac64`, which is a
    *non-standard* ISAAC-64: it is seeded from a decimal STRING via an undisclosed
    transform, and is NOT bit-compatible with the reference / rand_isaac ISAAC-64
    (verified against WeChat's shipped wasm_video_decode.wasm: seed "0" yields
    0x9d39247e33776d41, which no correct stock ISAAC-64 produces).

    Concretely, reproducing decryption here was NOT achieved:
      * Seeding WeChat's real WASM with this media's url `key` decimal does not
        decrypt its ciphertext (no JPEG magic, md5 mismatch); candidate seeds
        (key, md5, media id, tokens) all fail too.
      * The encrypted length (e.g. 148952B) exceeds the declared plaintext
        totalSize (147782B), which is inconsistent with a pure 1:1 stream XOR —
        so the on-CDN format and/or the per-image seed derivation differ from the
        documented 视频号 (Channels) scheme and need more reverse-engineering.

    Until that is resolved, an encrypted image degrades to "unread" rather than
    feeding garbage to the vision model. Plaintext feeds (key="0", no x-Enc) are
    the common case and already work end-to-end.
    """
    return None, None


def fetch_image(source):
    """Download and (if needed) decrypt one image descriptor → (bytes, mime) or None."""
    url = source.get('url', '')
    if not _host_ok(url):
        return None
    # Encrypted feeds require the token (and enc_idx) as query params or the CDN 400s.
    token = source.get('token', '')
    if token:
        sep = '&' if '?' in url else '?'
        url = '%s%stoken=%s&idx=%s' % (url, sep, urllib.parse.quote(token, safe=''),
                                       source.get('idx') or '1')
    req = urllib.request.Request(url, headers={'User-Agent': _UA})
    try:
        with _opener.open(req, timeout=_TIMEOUT) as resp:
            data = resp.read(MAX_IMAGE_BYTES + 1)
            headers = getattr(resp, 'headers', None)
            encrypted = bool(headers) and str(headers.get('x-Enc', '')).strip() == '1'
    except Exception:
        return None
    if not data or len(data) > MAX_IMAGE_BYTES:
        return None
    if not encrypted:
        mime = _valid_image(data)
        if mime:
            return data, mime
    data, mime = _sns_decrypt(data, source)
    if mime:
        return data, mime
    return None


def _unfence(text):
    """Strip an optional ```json ... ``` code fence the vision model may wrap around JSON."""
    body = text.strip()
    if body.startswith('```'):
        body = body.strip('`')
        body = body.split('\n', 1)[1] if '\n' in body else body
        if body.lstrip().lower().startswith('json'):
            body = body.lstrip()[4:]
    return body.strip()


def _describe(data, mime, cfg):
    try:
        text = llm.describe_image(data, media_type=mime, prompt=VISION_PROMPT, cfg=cfg, strict=True)
    except Exception:
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        payload = json.loads(_unfence(text))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get('status') != 'success':
        return None
    desc = payload.get('description')
    return desc.strip() if isinstance(desc, str) and desc.strip() else None


def describe_feed_images(feed_id, cfg=None, limit=2):
    """Return up to `limit` visual descriptions for a feed's pictures.

    Best-effort evidence: any download/decrypt/model failure yields fewer (or no)
    descriptions rather than an exception, so a comment can still be skipped
    honestly. Successful descriptions are cached per (account, feed, media).
    """
    cfg = cfg or llm.load_cfg()
    account = sessions.check()['account']
    try:
        xml_str = m.raw_content(feed_id)
    except Exception:
        return []
    if not xml_str:
        return []
    results = []
    for source in image_sources(xml_str)[:limit]:
        ckey = (account, str(feed_id), source['id'])
        with _cache_lock:
            cached = _desc_cache.get(ckey)
        if cached is not None:
            if cached:
                results.append(cached)
            continue
        fetched = fetch_image(source)
        desc = _describe(*fetched, cfg) if fetched else None
        with _cache_lock:
            if len(_desc_cache) >= _CACHE_MAX:
                _desc_cache.clear()
            _desc_cache[ckey] = desc or ''
        if desc:
            results.append(desc)
    return results
