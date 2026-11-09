"""Platform-neutral data model shared by every connector and analysis step."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def to_utc(value) -> Optional[datetime]:
    """Parse ISO strings / naive datetimes into timezone-aware UTC datetimes."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


@dataclass
class Media:
    type: str                      # "photo" | "video" | "thumbnail" | "document"
    url: Optional[str] = None
    phash: Optional[str] = None    # perceptual hash, used for image reuse matching


@dataclass
class Engagement:
    views: Optional[int] = None
    likes: Optional[int] = None
    comments: Optional[int] = None
    shares: Optional[int] = None     # Telegram: forwards
    reactions: Optional[int] = None

    def score(self) -> int:
        """Single comparable reach number (views are discounted, they dwarf the rest)."""
        active = sum(v or 0 for v in (self.likes, self.comments, self.shares, self.reactions))
        return active + (self.views or 0) // 100


@dataclass
class Post:
    id: str                          # "<platform>:<...>" globally unique
    platform: str                    # "youtube" | "telegram"
    kind: str                        # "video" | "comment" | "message"
    url: str
    author_id: str                   # Account.id
    text: str
    created_at: datetime
    media: list[Media] = field(default_factory=list)
    engagement: Engagement = field(default_factory=Engagement)
    reply_to: Optional[str] = None           # post this replies to / comments on
    share_of: Optional[str] = None           # post this forwards / reshares
    share_of_author: Optional[str] = None    # account that originally posted share_of
    share_of_date: Optional[datetime] = None # original post time (from forward header)
    urls: list[str] = field(default_factory=list)
    matched_query: bool = True               # text itself mentions the topic
    available: bool = True                   # False if the post was gone at the last refresh
    engagement_history: list[dict] = field(default_factory=list)  # [{"at": iso, views, likes, ...}]

    def record_engagement(self, new: "Engagement", at: datetime, first_seen_at: Optional[datetime]) -> None:
        """Keep the collection-time snapshot, then store the new numbers."""
        if not self.engagement_history:
            self.engagement_history.append({"at": iso(first_seen_at), **asdict(self.engagement)})
        self.engagement = new
        self.available = True
        self.engagement_history.append({"at": iso(at), **asdict(new)})

    def to_dict(self) -> dict:
        d = asdict(self)
        d["created_at"] = iso(self.created_at)
        d["share_of_date"] = iso(self.share_of_date)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Post":
        d = dict(d)
        d["created_at"] = to_utc(d["created_at"])
        d["share_of_date"] = to_utc(d.get("share_of_date"))
        d["media"] = [Media(**m) for m in d.get("media", [])]
        d["engagement"] = Engagement(**d.get("engagement", {}))
        return cls(**d)


@dataclass
class Account:
    id: str
    platform: str
    handle: Optional[str] = None
    name: Optional[str] = None
    url: Optional[str] = None
    created_at: Optional[datetime] = None
    avatar_url: Optional[str] = None
    avatar_phash: Optional[str] = None
    followers: Optional[int] = None
    kind: str = "user"               # "user" | "channel" | "group" | "bot"

    def merge(self, other: "Account") -> None:
        """Fill fields that are still empty with values from another record of the same account."""
        for f in fields(self):
            if getattr(self, f.name) in (None, "") and getattr(other, f.name) not in (None, ""):
                setattr(self, f.name, getattr(other, f.name))

    def label(self) -> str:
        return self.handle or self.name or self.id

    def to_dict(self) -> dict:
        d = asdict(self)
        d["created_at"] = iso(self.created_at)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Account":
        d = dict(d)
        d["created_at"] = to_utc(d.get("created_at"))
        return cls(**d)


class Dataset:
    """In-memory store of collected posts and accounts, persisted as JSONL."""

    def __init__(self) -> None:
        self.posts: dict[str, Post] = {}
        self.accounts: dict[str, Account] = {}
        self.meta: dict = {}

    def add_post(self, post: Post) -> None:
        self.posts[post.id] = post

    def add_account(self, account: Account) -> None:
        if account.id in self.accounts:
            self.accounts[account.id].merge(account)
        else:
            self.accounts[account.id] = account

    def account_label(self, account_id: str) -> str:
        acc = self.accounts.get(account_id)
        return acc.label() if acc else account_id

    def save(self, directory: str | Path) -> None:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        with open(directory / "posts.jsonl", "w", encoding="utf-8") as fh:
            for p in sorted(self.posts.values(), key=lambda p: p.created_at):
                fh.write(json.dumps(p.to_dict(), ensure_ascii=False) + "\n")
        with open(directory / "accounts.jsonl", "w", encoding="utf-8") as fh:
            for a in self.accounts.values():
                fh.write(json.dumps(a.to_dict(), ensure_ascii=False) + "\n")
        (directory / "meta.json").write_text(json.dumps(self.meta, indent=2, default=str))

    @classmethod
    def load(cls, directory: str | Path) -> "Dataset":
        directory = Path(directory)
        ds = cls()
        with open(directory / "posts.jsonl", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    ds.add_post(Post.from_dict(json.loads(line)))
        with open(directory / "accounts.jsonl", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    ds.add_account(Account.from_dict(json.loads(line)))
        meta = directory / "meta.json"
        if meta.exists():
            ds.meta = json.loads(meta.read_text())
        return ds
