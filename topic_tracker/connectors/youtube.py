"""YouTube Data API v3 connector (free API key).

Quota (default 10,000 units/day):
  search.list          100 units per call   <- the expensive one, budgeted by max_search_calls
  videos.list            1 unit per 50 videos
  commentThreads.list    1 unit per 100 comments
  channels.list          1 unit per 50 channels
"""
from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from datetime import datetime, timedelta
from typing import Callable, Iterable, Optional

import requests

from ..models import Account, Dataset, Engagement, Media, Post, to_utc
from ..text import TopicQuery, extract_urls
from .base import Connector

log = logging.getLogger(__name__)
API = "https://www.googleapis.com/youtube/v3"


class QuotaExceeded(RuntimeError):
    pass


class YouTubeAPIError(RuntimeError):
    """An API error whose message never contains the request URL (which carries the API key)."""


_KEY_RE = re.compile(r"key=[^&\s'\"]+")


def redact(text: str) -> str:
    return _KEY_RE.sub("key=[hidden]", str(text))


def _rfc3339(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


class YouTubeConnector(Connector):
    platform = "youtube"

    def __init__(self, api_key: str, max_videos: int = 300, max_search_calls: int = 40,
                 pages_per_window: int = 4, include_comments: bool = True,
                 max_comments_per_video: int = 300, session: Optional[requests.Session] = None,
                 workers: int = 8):
        self.api_key = api_key
        self.max_videos = max_videos
        self.max_search_calls = max_search_calls
        self.pages_per_window = pages_per_window
        self.include_comments = include_comments
        self.max_comments_per_video = max_comments_per_video
        self._shared_session = session        # tests inject one; otherwise one session per thread
        self._local = threading.local()
        self.workers = workers
        self._lock = threading.Lock()         # guards the dataset, channel set and quota counter
        self._channels: set[str] = set()
        self.units_used = 0

    @property
    def session(self):
        if self._shared_session is not None:
            return self._shared_session
        if not hasattr(self._local, "session"):
            self._local.session = requests.Session()
        return self._local.session

    def _parallel(self, fn: Callable, items: Iterable, progress: Optional[str] = None) -> None:
        """Run fn over items on `workers` threads. Stops early (and re-raises) on the first error,
        e.g. when the daily quota runs out, so no further quota is spent."""
        items = list(items)
        if not items:
            return
        done = 0
        with ThreadPoolExecutor(self.workers) as ex:
            pending = {ex.submit(fn, it) for it in items}
            while pending:
                finished, pending = wait(pending, return_when=FIRST_EXCEPTION)
                for f in finished:
                    if f.exception() is not None:
                        for other in pending:
                            other.cancel()
                        raise f.exception()
                    done += 1
                if progress and (done % 25 == 0 or not pending or len(finished) > 1):
                    log.info(progress, done, len(items))

    # ---------------------------------------------------------------- HTTP
    def _get(self, endpoint: str, cost: int = 1, **params) -> Optional[dict]:
        params = {k: v for k, v in params.items() if v is not None}
        params["key"] = self.api_key
        for attempt in range(5):
            try:
                r = self.session.get(f"{API}/{endpoint}", params=params, timeout=30)
            except requests.RequestException as e:   # network errors include the URL, so redact
                if attempt < 4:
                    time.sleep(2 ** attempt)
                    continue
                raise YouTubeAPIError(f"Could not reach YouTube ({endpoint}): {redact(e)}") from None
            if r.status_code == 200:
                with self._lock:
                    self.units_used += cost
                return r.json()
            reason, message = "", ""
            try:
                err = r.json()["error"]
                message = err.get("message", "")
                reason = err["errors"][0]["reason"]
            except (ValueError, KeyError, IndexError, TypeError):
                pass
            if reason in ("quotaExceeded", "dailyLimitExceeded"):
                raise QuotaExceeded(f"YouTube quota exhausted after ~{self.units_used} units")
            if reason in ("commentsDisabled", "videoNotFound", "forbidden") or r.status_code == 404:
                return None
            if r.status_code in (429, 500, 502, 503):
                time.sleep(2 ** attempt)
                continue
            raise YouTubeAPIError(f"YouTube returned error {r.status_code} for {endpoint}: "
                                  f"{redact(message or reason or 'no details')}")
        return None

    # ---------------------------------------------------------------- main
    def collect(self, query: TopicQuery, dataset: Dataset) -> None:
        q = "|".join(f'"{v}"' if " " in v else v for v in query.variants())  # YouTube OR syntax
        try:
            video_ids = self._search(q, query.since, query.until)
            log.info("YouTube: %d candidate videos", len(video_ids))
            videos = self._videos(video_ids, query, dataset)
            if self.include_comments:   # several videos at once
                self._parallel(lambda post: self._comments(post, query, dataset), videos,
                               "YouTube: comments collected for %d of %d videos")
            self._fetch_channels(dataset)
        except QuotaExceeded as e:
            log.warning("%s - keeping what was collected so far", e)
        log.info("YouTube: ~%d quota units used", self.units_used)

    def refresh(self, dataset: Dataset, now: datetime) -> None:
        """Current views/likes/comments for collected videos and likes for comments (1 unit per 50)."""
        first_seen = to_utc(dataset.meta.get("collected_at"))
        posts = [p for p in dataset.posts.values() if p.platform == "youtube"]
        jobs = [("videos", "statistics", [p for p in posts if p.kind == "video"]),
                ("comments", "snippet", [p for p in posts if p.kind == "comment"])]
        try:
            for endpoint, part, items in jobs:
                for batch in _chunks(items, 50):
                    by_id = {p.id.rsplit(":", 1)[1]: p for p in batch}
                    extra = {"textFormat": "plainText"} if endpoint == "comments" else {}
                    try:   # note: comments.list rejects maxResults together with id
                        data = self._get(endpoint, part=part, id=",".join(by_id), **extra)
                    except YouTubeAPIError as e:
                        log.warning("%s - skipped %d posts", e, len(by_id))
                        continue
                    found = set()
                    for item in (data or {}).get("items", []):
                        post = by_id.get(item["id"])
                        if not post:
                            continue
                        found.add(item["id"])
                        if endpoint == "videos":
                            st = item.get("statistics", {})
                            new = Engagement(views=_int(st.get("viewCount")), likes=_int(st.get("likeCount")),
                                             comments=_int(st.get("commentCount")))
                        else:
                            new = Engagement(likes=_int(item["snippet"].get("likeCount")))
                        post.record_engagement(new, now, first_seen)
                    if data is not None:
                        for vid, post in by_id.items():
                            if vid not in found:
                                post.available = False   # deleted, made private or removed
        except QuotaExceeded as e:
            log.warning("%s - refresh stopped early", e)
        log.info("YouTube refresh: ~%d quota units used", self.units_used)

    def _search(self, q: str, since: datetime, until: datetime) -> list[str]:
        """search.list returns at most ~500 results per query, so busy windows are split in half
        until each fits. Earlier windows are searched first so the budget favours origins."""
        ids: dict[str, None] = {}
        stack = [(since, until)]
        calls = 0
        cap = self.pages_per_window * 50
        while stack and len(ids) < self.max_videos:
            start, end = stack.pop()
            page, pages = None, 0
            while True:
                if calls >= self.max_search_calls:
                    log.warning("YouTube: search budget (%d calls) reached", calls)
                    return list(ids)[: self.max_videos]
                data = self._get("search", cost=100, part="id", q=q, type="video", order="date",
                                 publishedAfter=_rfc3339(start), publishedBefore=_rfc3339(end),
                                 maxResults=50, pageToken=page)
                calls += 1
                if not data:
                    break
                for item in data.get("items", []):
                    vid = item.get("id", {}).get("videoId")
                    if vid:
                        ids.setdefault(vid)
                total = data.get("pageInfo", {}).get("totalResults", 0)
                if pages == 0 and total > cap and end - start > timedelta(hours=2):
                    mid = start + (end - start) / 2
                    stack.append((mid, end))      # later half
                    stack.append((start, mid))    # earlier half popped first
                    break
                page = data.get("nextPageToken")
                pages += 1
                if not page or pages >= self.pages_per_window:
                    break
        return list(ids)[: self.max_videos]

    def _videos(self, ids: list[str], query: TopicQuery, dataset: Dataset) -> list[Post]:
        out = []
        for batch in _chunks(ids, 50):
            data = self._get("videos", part="snippet,statistics", id=",".join(batch))
            for v in (data or {}).get("items", []):
                sn, st = v["snippet"], v.get("statistics", {})
                created = to_utc(sn["publishedAt"])
                if not (query.since <= created <= query.until):
                    continue
                text = f"{sn.get('title', '')}\n\n{sn.get('description', '')}".strip()
                thumbs = sn.get("thumbnails", {})
                thumb = (thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}).get("url")
                url = f"https://www.youtube.com/watch?v={v['id']}"
                post = Post(
                    id=f"youtube:video:{v['id']}", platform="youtube", kind="video", url=url,
                    author_id=f"youtube:{sn['channelId']}", text=text, created_at=created,
                    media=[Media("video", url)] + ([Media("thumbnail", thumb)] if thumb else []),
                    engagement=Engagement(views=_int(st.get("viewCount")), likes=_int(st.get("likeCount")),
                                          comments=_int(st.get("commentCount"))),
                    urls=extract_urls(sn.get("description", "")),
                    matched_query=query.matches(text + " " + " ".join(sn.get("tags", []))),
                )
                dataset.add_post(post)
                self._channels.add(sn["channelId"])
                out.append(post)
        return out

    def _comments(self, video: Post, query: TopicQuery, dataset: Dataset) -> None:
        video_id = video.id.split(":")[-1]
        page, seen = None, 0
        while seen < self.max_comments_per_video:
            data = self._get("commentThreads", part="snippet,replies", videoId=video_id, maxResults=100,
                             order="time", textFormat="plainText", pageToken=page)
            if not data:
                return
            all_older = True
            for thread in data.get("items", []):
                top = thread["snippet"]["topLevelComment"]
                if self._add_comment(top, video_id, video.id, query, dataset):
                    all_older = False
                for reply in (thread.get("replies") or {}).get("comments", []):
                    self._add_comment(reply, video_id, f"youtube:comment:{top['id']}", query, dataset)
                seen += 1
            page = data.get("nextPageToken")
            if not page or all_older:   # newest-first: once a page is all too old, stop
                return

    def _add_comment(self, c: dict, video_id: str, parent: str, query: TopicQuery, dataset: Dataset) -> bool:
        s = c["snippet"]
        created = to_utc(s["publishedAt"])
        if created < query.since:
            return False
        if created > query.until:
            return True
        channel = (s.get("authorChannelId") or {}).get("value")
        author_id = f"youtube:{channel}" if channel else f"youtube:anon:{s.get('authorDisplayName')}"
        text = s.get("textOriginal") or s.get("textDisplay") or ""
        post = Post(
            id=f"youtube:comment:{c['id']}", platform="youtube", kind="comment",
            url=f"https://www.youtube.com/watch?v={video_id}&lc={c['id']}", author_id=author_id,
            text=text, created_at=created, engagement=Engagement(likes=_int(s.get("likeCount"))),
            reply_to=parent, urls=extract_urls(text), matched_query=query.matches(text),
        )
        account = Account(id=author_id, platform="youtube", name=s.get("authorDisplayName"),
                          url=s.get("authorChannelUrl"), avatar_url=s.get("authorProfileImageUrl"))
        with self._lock:
            dataset.add_post(post)
            dataset.add_account(account)
            if channel:
                self._channels.add(channel)
        return True

    def _fetch_channels(self, dataset: Dataset) -> None:
        """Channel creation date, handle, subscribers and avatar for every author seen."""
        def fetch(batch):
            data = self._get("channels", part="snippet,statistics", id=",".join(batch))
            with self._lock:
                self._store_channels(data, dataset)

        self._parallel(fetch, _chunks(sorted(self._channels), 50))

    def _store_channels(self, data, dataset: Dataset) -> None:
        for ch in (data or {}).get("items", []):
            sn, st = ch["snippet"], ch.get("statistics", {})
            dataset.add_account(Account(
                id=f"youtube:{ch['id']}", platform="youtube", handle=sn.get("customUrl"),
                name=sn.get("title"), url=f"https://www.youtube.com/channel/{ch['id']}",
                created_at=to_utc(sn.get("publishedAt")),
                avatar_url=(sn.get("thumbnails", {}).get("default") or {}).get("url"),
                followers=None if st.get("hiddenSubscriberCount") else _int(st.get("subscriberCount")),
                kind="channel",
            ))
