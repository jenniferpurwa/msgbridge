"""Shared links: keep the URL, minus tracking, unless the link itself grants access.

Links that open someone's documents, meetings or payments (a Google Docs share
link, a Zoom link with its password) work for whoever holds them, so those
become "[private link: docs.google.com]" instead of being passed to an AI.
"""
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

URL_RE = re.compile(r"https?://[^\s<>\"']+")
TRAILING = ".,;:!?)]}'\""  # punctuation that usually ends the sentence, not the URL

TRACKING_PARAMS = {"fbclid", "gclid", "dclid", "msclkid", "igshid", "igsh", "mc_cid", "mc_eid", "_hsenc", "_hsmi",
                   "ref_src", "ref_url", "mibextid", "rdt", "share_id"}
SHARE_ID_HOSTS = {"youtube.com", "youtu.be", "open.spotify.com", "music.apple.com"}  # their "si" is a sharer id

ACCESS_HOSTS = {"docs.google.com", "drive.google.com", "photos.app.goo.gl", "photos.google.com", "meet.google.com",
                "zoom.us", "dropbox.com", "icloud.com", "share.icloud.com", "1drv.ms", "onedrive.live.com",
                "venmo.com", "account.venmo.com", "paypal.me", "paypal.com", "cash.app", "wise.com", "splitwise.com",
                "calendly.com", "docusign.net", "app.docusign.com", "notion.so", "airtable.com", "figma.com"}
ACCESS_PARAMS = re.compile(r"(^|_)(pwd|pass|password|token|access_token|key|sig|signature|secret|auth|code|invite)$", re.I)


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _is_access_link(host: str, params: list[tuple[str, str]]) -> bool:
    if any(host == h or host.endswith("." + h) for h in ACCESS_HOSTS):
        return True
    return any(ACCESS_PARAMS.search(k) for k, _ in params)


def clean(url: str) -> str:
    """The URL without tracking parameters, or a private-link placeholder."""
    parts = urlparse(url)
    host = host_of(url)
    params = parse_qsl(parts.query, keep_blank_values=True)
    if _is_access_link(host, params):
        return f"[private link: {host}]"
    kept = [(k, v) for k, v in params
            if not (k.lower().startswith("utm_") or k.lower() in TRACKING_PARAMS
                    or (k == "si" and host in SHARE_ID_HOSTS))]
    return urlunparse(parts._replace(query=urlencode(kept, doseq=True)))


def clean_text(text: str) -> str:
    """Clean every URL inside a message, leaving sentence punctuation outside the link."""
    def one(m: re.Match) -> str:
        url = m.group(0)
        tail = ""
        while url and url[-1] in TRAILING and not (url[-1] == ")" and url.count("(") >= url.count(")")):
            tail = url[-1] + tail
            url = url[:-1]
        return clean(url) + tail
    return URL_RE.sub(one, text)
