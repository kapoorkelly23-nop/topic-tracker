"""Perceptual hashing for profile photos and post images."""
from __future__ import annotations

import io
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterable, Optional

import imagehash
import requests
from PIL import Image

from .models import Dataset

log = logging.getLogger(__name__)


def phash_bytes(data: Optional[bytes]) -> Optional[str]:
    """64-bit pHash as 16 hex chars. Robust to resizing, recompression and small edits."""
    if not data:
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            return str(imagehash.phash(im.convert("RGB")))
    except Exception:  # corrupt / unsupported image
        return None


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


CACHE_PATH = Path.home() / ".cache" / "topic_tracker" / "avatars.json"


def _load_cache(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _save_cache(path: Path, cache: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache))
    except OSError:
        pass


def enrich_avatars(dataset: Dataset, account_ids: Optional[Iterable[str]] = None, workers: int = 32,
                   timeout: int = 10, cache_path: Optional[Path] = CACHE_PATH) -> int:
    """Download profile photos that only have a URL (YouTube) and store their pHash.

    Only the accounts in `account_ids` are checked (all if None). Results are cached by URL, so
    accounts seen in earlier searches are not downloaded again.
    """
    wanted = set(account_ids) if account_ids is not None else None
    todo = [a for a in dataset.accounts.values()
            if a.avatar_url and not a.avatar_phash and (wanted is None or a.id in wanted)]
    cache = _load_cache(cache_path) if cache_path else {}
    fetch_list = []
    for acc in todo:
        if acc.avatar_url in cache:
            acc.avatar_phash = cache[acc.avatar_url] or None
        else:
            fetch_list.append(acc)
    log.info("Hashing %d profile photos (%d already known)", len(todo), len(todo) - len(fetch_list))

    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=workers, pool_maxsize=workers)
    session.mount("https://", adapter)

    def fetch(acc):
        try:
            r = session.get(acc.avatar_url, timeout=timeout)
            r.raise_for_status()
            return acc, phash_bytes(r.content)
        except requests.RequestException:
            return acc, None

    done = 0
    with ThreadPoolExecutor(workers) as ex:
        for acc, h in ex.map(fetch, fetch_list):
            cache[acc.avatar_url] = h or ""
            if h:
                acc.avatar_phash = h
                done += 1
    if cache_path and fetch_list:
        _save_cache(cache_path, cache)
    log.info("Hashed %d new profile photos", done)
    return done
