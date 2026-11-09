"""Telegram connector using the free MTProto API through Telethon.

Telegram has no global keyword search over all public posts, so collection works on chats:
  1. seed chats you pass in (usernames or t.me links),
  2. public channels/groups whose *name* matches the topic (contacts.Search),
  3. snowballing: channels that seed chats forward from or link to (depth-limited).
Inside each chat the topic is searched server-side (messages.search).

Notes
- First run asks for your phone number and login code in the terminal; the session file
  is reused afterwards.
- FloodWait errors (rate limits) are slept through automatically up to `flood_sleep` seconds.
- Telegram does not expose user account creation dates. For channels, `Channel.date` is the
  creation date only when you are not a member (otherwise it is your join date).
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from datetime import datetime
from typing import Iterable, Optional

from ..images import phash_bytes
from ..models import Account, Dataset, Engagement, Media, Post, to_utc
from ..text import TopicQuery, extract_urls, parse_ref
from .base import Connector

log = logging.getLogger(__name__)


def _engagement(msg) -> Engagement:
    reactions = None
    if msg.reactions and msg.reactions.results:
        reactions = sum(r.count for r in msg.reactions.results)
    return Engagement(views=msg.views, shares=msg.forwards, reactions=reactions,
                      comments=msg.replies.replies if msg.replies else None)


def _clean_ref(ref: str) -> str:
    parsed = parse_ref(ref) if "/" in ref else None
    return parsed[1] if parsed and parsed[0] == "telegram" else ref.lstrip("@")


class TelegramLoginRequired(RuntimeError):
    pass


class TelegramConnector(Connector):
    platform = "telegram"

    def __init__(self, api_id: int, api_hash: str, session: str = "topic_tracker",
                 channels: Iterable[str] = (), discover: bool = True, discover_limit: int = 20,
                 snowball_depth: int = 1, max_chats: int = 60, max_messages_per_chat: int = 3000,
                 download_avatars: bool = True, download_photos: bool = False, flood_sleep: int = 300,
                 interactive: bool = True):
        self.api_id = int(api_id)
        self.api_hash = api_hash
        self.session = session
        self.channels = [_clean_ref(c) for c in channels if c.strip()]
        self.discover = discover
        self.discover_limit = discover_limit
        self.snowball_depth = snowball_depth
        self.max_chats = max_chats
        self.max_messages_per_chat = max_messages_per_chat
        self.download_avatars = download_avatars
        self.download_photos = download_photos
        self.flood_sleep = flood_sleep
        self.interactive = interactive   # False when run from the search page: never prompt
        self._accounts: dict[int, Account] = {}

    async def _client(self):
        """Connected client. Logs in interactively in the terminal only when allowed."""
        from telethon import TelegramClient

        client = TelegramClient(self.session, self.api_id, self.api_hash, flood_sleep_threshold=self.flood_sleep)
        if self.interactive:
            await client.start()
            return client
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise TelegramLoginRequired(
                "Telegram is not logged in yet. Run this once in the terminal: python -m topic_tracker telegram-login")
        return client

    def login(self) -> str:
        async def run():
            client = await self._client()
            me = await client.get_me()
            await client.disconnect()
            return me.first_name or me.username or "your account"
        return asyncio.run(run())

    def collect(self, query: TopicQuery, dataset: Dataset) -> None:
        asyncio.run(self._collect(query, dataset))

    def refresh(self, dataset: Dataset, now: datetime) -> None:
        asyncio.run(self._refresh(dataset, now))

    async def _refresh(self, dataset: Dataset, now: datetime) -> None:
        """Current views/forwards/reactions/replies for collected messages, chat by chat."""
        first_seen = to_utc(dataset.meta.get("collected_at"))
        by_chat = defaultdict(list)
        for p in dataset.posts.values():
            if p.platform == "telegram":
                _, chat, msg_id = p.id.split(":")
                by_chat[int(chat)].append((int(msg_id), p))
        client = await self._client()
        try:
            for chat, items in by_chat.items():
                acc = dataset.accounts.get(f"telegram:{chat}")
                try:
                    entity = await client.get_entity(acc.handle if acc and acc.handle else chat)
                except Exception as e:  # noqa: BLE001
                    log.warning("Telegram refresh: cannot open chat %s: %s", chat, e)
                    continue
                for i in range(0, len(items), 100):
                    batch = items[i:i + 100]
                    msgs = await client.get_messages(entity, ids=[m for m, _ in batch])
                    for (_, post), msg in zip(batch, msgs):
                        if msg is None:
                            post.available = False   # deleted
                        else:
                            post.record_engagement(_engagement(msg), now, first_seen)
        finally:
            await client.disconnect()

    async def _collect(self, query: TopicQuery, dataset: Dataset) -> None:
        from telethon import functions, types, utils

        client = await self._client()
        try:
            queue: deque = deque((name, 0) for name in self.channels)
            if self.discover:
                for term in query.variants():
                    try:
                        res = await client(functions.contacts.SearchRequest(q=term.lstrip("#@"),
                                                                            limit=self.discover_limit))
                    except Exception as e:  # noqa: BLE001 - discovery is best effort
                        log.warning("Telegram discovery for %r failed: %s", term, e)
                        continue
                    for chat in res.chats:
                        if isinstance(chat, types.Channel) and chat.username:
                            queue.append((chat, 0))

            seen: set[int] = set()
            while queue and len(seen) < self.max_chats:
                ref, depth = queue.popleft()
                try:
                    entity = await client.get_entity(ref) if isinstance(ref, str) else ref
                except Exception as e:  # noqa: BLE001 - private, renamed or invalid chats
                    log.warning("Telegram: cannot open %r: %s", ref, e)
                    continue
                peer = utils.get_peer_id(entity)
                if peer in seen:
                    continue
                seen.add(peer)
                dataset.add_account(await self._account(client, entity))
                new_refs = await self._collect_chat(client, entity, query, dataset)
                log.info("Telegram: %s done (%d chats so far)", getattr(entity, "username", peer), len(seen))
                if depth < self.snowball_depth:
                    queue.extend((r, depth + 1) for r in new_refs)
        finally:
            await client.disconnect()

    async def _collect_chat(self, client, entity, query: TopicQuery, dataset: Dataset) -> set:
        from telethon import types, utils

        chat_peer = utils.get_peer_id(entity)
        refs: set = set()
        for term in query.variants():
            count = 0
            async for msg in client.iter_messages(entity, search=term, offset_date=query.until):
                if msg.date < query.since or count >= self.max_messages_per_chat:
                    break
                count += 1
                if isinstance(msg, types.MessageService):
                    continue
                if f"telegram:{chat_peer}:{msg.id}" in dataset.posts:
                    continue
                post = await self._to_post(client, entity, msg, query, dataset, refs)
                dataset.add_post(post)
        return refs

    async def _to_post(self, client, entity, msg, query: TopicQuery, dataset: Dataset, refs: set) -> Post:
        from telethon import types, utils

        chat_peer = utils.get_peer_id(entity)
        username = getattr(entity, "username", None)
        url = f"https://t.me/{username}/{msg.id}" if username else f"https://t.me/c/{entity.id}/{msg.id}"

        # Author: the channel itself for broadcast channels, the sender in groups.
        author_id = f"telegram:{chat_peer}"
        if not getattr(entity, "broadcast", False):
            sender = await msg.get_sender()
            if sender is not None:
                acc = await self._account(client, sender)
                dataset.add_account(acc)
                author_id = acc.id

        # Forward header: records the original source even if we never collect it.
        share_of = share_of_author = share_of_date = None
        if msg.fwd_from:
            share_of_date = to_utc(msg.fwd_from.date)
            if msg.fwd_from.from_id is not None:
                src_peer = utils.get_peer_id(msg.fwd_from.from_id)
                share_of_author = f"telegram:{src_peer}"
                if msg.fwd_from.channel_post:
                    share_of = f"telegram:{src_peer}:{msg.fwd_from.channel_post}"
                try:
                    src = await msg.forward.get_chat() or await msg.forward.get_sender()
                except Exception:  # noqa: BLE001
                    src = None
                if src is not None:
                    dataset.add_account(await self._account(client, src))
                    if isinstance(src, types.Channel) and src.username:
                        refs.add(src)
            elif msg.fwd_from.from_name:   # user hid their account when forwarded
                share_of_author = f"telegram:hidden:{msg.fwd_from.from_name}"
                dataset.add_account(Account(id=share_of_author, platform="telegram", name=msg.fwd_from.from_name))

        reply_to = None
        if msg.reply_to and getattr(msg.reply_to, "reply_to_msg_id", None):
            reply_to = f"telegram:{chat_peer}:{msg.reply_to.reply_to_msg_id}"

        text = msg.message or ""
        urls = extract_urls(text) + [e.url for e in (msg.entities or []) if getattr(e, "url", None)]
        urls = list(dict.fromkeys(urls))
        for u in urls:
            ref = parse_ref(u)
            if ref and ref[0] == "telegram" and ref[1] != (username or "").lower():
                refs.add(ref[1])

        media: list[Media] = []
        if msg.photo:
            h = None
            if self.download_photos:
                try:
                    h = phash_bytes(await client.download_media(msg, file=bytes))
                except Exception:  # noqa: BLE001
                    pass
            media.append(Media("photo", url, h))
        if msg.video or msg.gif:
            media.append(Media("video", url))

        return Post(
            id=f"telegram:{chat_peer}:{msg.id}", platform="telegram", kind="message", url=url,
            author_id=author_id, text=text, created_at=to_utc(msg.date), media=media,
            engagement=_engagement(msg), reply_to=reply_to, share_of=share_of, share_of_author=share_of_author,
            share_of_date=share_of_date, urls=urls, matched_query=query.matches(text),
        )

    async def _account(self, client, entity) -> Account:
        from telethon import types, utils

        peer = utils.get_peer_id(entity)
        if peer in self._accounts:
            return self._accounts[peer]
        if isinstance(entity, types.User):
            name = " ".join(filter(None, [entity.first_name, entity.last_name])) or None
            kind, created, followers = ("bot" if entity.bot else "user"), None, None
        else:
            name = getattr(entity, "title", None)
            kind = "channel" if getattr(entity, "broadcast", False) else "group"
            created, followers = to_utc(getattr(entity, "date", None)), getattr(entity, "participants_count", None)
        handle = getattr(entity, "username", None)

        avatar_hash: Optional[str] = None
        photo = getattr(entity, "photo", None)
        if self.download_avatars and photo and not isinstance(
                photo, (types.UserProfilePhotoEmpty, types.ChatPhotoEmpty)):
            try:
                avatar_hash = phash_bytes(await client.download_profile_photo(entity, file=bytes))
            except Exception:  # noqa: BLE001
                pass

        acc = Account(id=f"telegram:{peer}", platform="telegram", handle=handle, name=name,
                      url=f"https://t.me/{handle}" if handle else None, created_at=created,
                      avatar_phash=avatar_hash, followers=followers, kind=kind)
        self._accounts[peer] = acc
        return acc
