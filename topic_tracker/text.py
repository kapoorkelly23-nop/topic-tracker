"""Topic query, text normalisation and cross-platform link parsing."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qs, urlparse

URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
MENTION_RE = re.compile(r"(?<!\w)@\w+")
NON_WORD_RE = re.compile(r"[^\w\s#]+", re.UNICODE)
WS_RE = re.compile(r"\s+")
CAMEL_RE = re.compile(r"(?<=[a-z])(?=[A-Z])")


@dataclass
class TopicQuery:
    topic: str            # keyword, "#hashtag", "@person", brand or event
    since: datetime
    until: datetime

    def variants(self) -> list[str]:
        """Search strings to try: '#AcmeBankCollapse' -> also 'AcmeBankCollapse', 'Acme Bank Collapse'."""
        t = self.topic.strip()
        out = [t]
        bare = t.lstrip("#@")
        out.append(bare)
        split = CAMEL_RE.sub(" ", bare)
        out.append(split)
        if " " in bare:
            out.append("#" + bare.replace(" ", ""))
        return list(dict.fromkeys(v for v in out if v))

    def terms(self) -> set[str]:
        """Lower-case words of the topic, used to keep them out of narrative labels."""
        words = set()
        for v in self.variants():
            words.update(w.lower() for w in re.findall(r"\w+", v))
        return words

    def matches(self, text: str) -> bool:
        low = (text or "").lower()
        return any(v.lower() in low for v in self.variants())


def extract_urls(text: str) -> list[str]:
    return list(dict.fromkeys(u.rstrip(".,;:!?") for u in URL_RE.findall(text or "")))


def canonical_url(url: str) -> str:
    u = urlparse(url)
    query = "&".join(p for p in u.query.split("&") if p and not p.startswith(("utm_", "si=", "fbclid")))
    return f"{u.netloc.lower().removeprefix('www.')}{u.path.rstrip('/')}" + (f"?{query}" if query else "")


def normalize(text: str) -> str:
    """Lowercase, strip URLs, @mentions, punctuation and emoji, collapse whitespace."""
    t = unicodedata.normalize("NFKC", text or "").lower()
    t = URL_RE.sub(" ", t)
    t = MENTION_RE.sub(" ", t)
    t = NON_WORD_RE.sub(" ", t)
    return WS_RE.sub(" ", t).strip()


def shingles(text: str, k: int = 5) -> set[str]:
    """Character k-grams of normalised text; language-agnostic (works for Hindi, Russian, ...)."""
    t = normalize(text)
    if len(t) <= k:
        return {t} if t else set()
    return {t[i:i + k] for i in range(len(t) - k + 1)}


_RESERVED_TME = {"joinchat", "addstickers", "addemoji", "share", "proxy", "socks", "iv", "c", "boost"}


def parse_ref(url: str) -> Optional[tuple]:
    """Identify links to the platforms we collect.

    Returns ("youtube", video_id) or ("telegram", username, message_id_or_None), else None.
    """
    try:
        u = urlparse(url if "://" in url else "https://" + url)
    except ValueError:
        return None
    host = (u.netloc or "").lower().removeprefix("www.").removeprefix("m.")
    if host == "youtu.be":
        vid = u.path.strip("/").split("/")[0]
        return ("youtube", vid) if vid else None
    if host.endswith("youtube.com"):
        if u.path == "/watch":
            vid = parse_qs(u.query).get("v", [None])[0]
            return ("youtube", vid) if vid else None
        parts = u.path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] in ("shorts", "live", "embed"):
            return ("youtube", parts[1])
        return None
    if host in ("t.me", "telegram.me"):
        parts = [p for p in u.path.split("/") if p]
        if parts and parts[0] == "s":
            parts = parts[1:]
        if not parts or parts[0].lower() in _RESERVED_TME or parts[0].startswith("+"):
            return None
        msg = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        return ("telegram", parts[0].lower(), msg)
    return None
