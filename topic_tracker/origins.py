"""Where each narrative started and how it spread across platforms.

For every narrative:
  first_posts        earliest posts from distinct accounts (who pushed it first)
  earlier_sources    older posts this narrative points back to, found through Telegram forward
                     headers or links, even when they sit outside the cluster or were never collected
  platform_path      first appearance per platform and the lag between them
  cross_platform     concrete hops: a post linking to another platform's post, or the same
                     text reappearing on another platform
  breakout           first volume spike and the high-reach posts just before/at it
Members of flagged coordinated groups are marked wherever they appear.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from statistics import median

from .models import Dataset, Post, iso
from .text import parse_ref


@dataclass
class OriginConfig:
    first_n: int = 5
    min_spike: int = 5
    spike_factor: float = 3.0


def _ref_index(ds: Dataset):
    handles = {a.handle.lower(): a.id.split(":", 1)[1] for a in ds.accounts.values()
               if a.platform == "telegram" and a.handle}

    def resolve(url: str):
        ref = parse_ref(url)
        if not ref:
            return None, None
        if ref[0] == "youtube":
            return "youtube", f"youtube:video:{ref[1]}"
        peer = handles.get(ref[1])
        if peer and ref[2]:
            return "telegram", f"telegram:{peer}:{ref[2]}"
        return "telegram", None
    return resolve


def _hours(a, b) -> float:
    return round((b - a).total_seconds() / 3600, 1)


def _brief(ds: Dataset, p: Post, group_of: dict) -> dict:
    return {"post_id": p.id, "url": p.url, "platform": p.platform, "time": iso(p.created_at),
            "account": ds.account_label(p.author_id), "account_id": p.author_id,
            "coordinated_group": group_of.get(p.author_id), "engagement": p.engagement.score(),
            "text": p.text[:200]}


def _breakout(ds, posts, group_of, cfg):
    span = posts[-1].created_at - posts[0].created_at
    step = timedelta(hours=1) if span <= timedelta(days=3) else \
        timedelta(hours=6) if span <= timedelta(days=14) else timedelta(days=1)
    start = posts[0].created_at
    bins = Counter(int((p.created_at - start) / step) for p in posts)
    counts = [bins.get(i, 0) for i in range(max(bins) + 1)]
    for i, c in enumerate(counts):
        if i == 0:
            continue
        base = median(counts[max(0, i - 24):i])
        if c >= max(cfg.min_spike, cfg.spike_factor * (base + 1)):
            t0 = start + i * step
            window = [p for p in posts if t0 - step <= p.created_at < t0 + step]
            top, seen = [], set()
            for p in sorted(window, key=lambda p: -p.engagement.score()):
                if p.author_id not in seen:
                    seen.add(p.author_id)
                    top.append(_brief(ds, p, group_of))
                if len(top) == 5:
                    break
            label = {timedelta(hours=1): "hour", timedelta(hours=6): "6 hours", timedelta(days=1): "day"}[step]
            return {"spike_start": iso(t0), "bin": label, "posts_in_bin": c,
                    "baseline_per_bin": base, "amplifiers": top}
    return None


def trace_origins(ds: Dataset, narratives: list[dict], clusters: list[list[str]],
                  groups: list[dict], cfg: OriginConfig) -> list[dict]:
    resolve = _ref_index(ds)
    group_of = {m["id"]: g["id"] for g in groups if g["flagged"] for m in g["members"]}
    cluster_of = {pid: i for i, c in enumerate(clusters) for pid in c}
    out = []
    for n in narratives:
        posts = sorted((ds.posts[i] for i in n["post_ids"]), key=lambda p: p.created_at)
        in_cluster = set(n["post_ids"])
        first, seen = [], set()
        for p in posts:
            if p.author_id not in seen:
                seen.add(p.author_id)
                first.append(_brief(ds, p, group_of))
            if len(first) == cfg.first_n:
                break

        earlier = {}
        t_first = posts[0].created_at
        for p in posts:
            if p.share_of_date and p.share_of_date < t_first:
                key = p.share_of or p.share_of_author
                src = ds.accounts.get(p.share_of_author or "")
                url = None
                if src and src.handle and p.share_of:
                    url = f"https://t.me/{src.handle}/{p.share_of.rsplit(':', 1)[1]}"
                earlier.setdefault(key, {"via": "forward", "time": iso(p.share_of_date),
                                         "post_id": p.share_of, "account": ds.account_label(p.share_of_author or ""),
                                         "coordinated_group": group_of.get(p.share_of_author),
                                         "url": url, "seen_in": p.url})
            for u in p.urls:
                _, target = resolve(u)
                src = ds.posts.get(target) if target else None
                if src and src.created_at < t_first and src.id not in in_cluster:
                    earlier.setdefault(src.id, {"via": "link", "time": iso(src.created_at), "post_id": src.id,
                                                "account": ds.account_label(src.author_id),
                                                "coordinated_group": group_of.get(src.author_id),
                                                "url": src.url, "seen_in": p.url})

        first_per_platform = {}
        for p in posts:
            first_per_platform.setdefault(p.platform, p)
        order = sorted(first_per_platform.values(), key=lambda p: p.created_at)
        path = [{"platform": p.platform, "first_seen": iso(p.created_at),
                 "lag_hours_from_start": _hours(order[0].created_at, p.created_at),
                 "first_post": _brief(ds, p, group_of)} for p in order]

        hops = []
        for p in posts:   # explicit links to the other platform
            for u in p.urls:
                platform, target = resolve(u)
                if platform and platform != p.platform:
                    src = ds.posts.get(target) if target else None
                    hops.append({
                        "via": "link", "from_platform": platform, "to_platform": p.platform,
                        "from": _brief(ds, src, group_of) if src else {"url": u, "collected": False},
                        "to": _brief(ds, p, group_of),
                        "lag_hours": _hours(src.created_at, p.created_at) if src else None})
        for ci in {cluster_of[i] for i in in_cluster if i in cluster_of}:   # same text elsewhere
            cposts = [ds.posts[i] for i in clusters[ci]]
            firsts = {}
            for p in cposts:
                firsts.setdefault(p.platform, p)
            if len(firsts) > 1:
                ordered = sorted(firsts.values(), key=lambda p: p.created_at)
                for later in ordered[1:]:
                    hops.append({"via": "same_text", "from_platform": ordered[0].platform,
                                 "to_platform": later.platform, "from": _brief(ds, ordered[0], group_of),
                                 "to": _brief(ds, later, group_of),
                                 "lag_hours": _hours(ordered[0].created_at, later.created_at)})
        hops.sort(key=lambda h: h["to"]["time"])

        coordinated_share = sum(1 for p in posts if p.author_id in group_of) / len(posts)
        out.append({
            "narrative_id": n["id"], "label": n["label"],
            "first_posts": first,
            "earlier_sources": sorted(earlier.values(), key=lambda e: e["time"])[:5],
            "platform_path": path,
            "cross_platform_hops": hops[:20],
            "breakout": _breakout(ds, posts, group_of, cfg),
            "share_of_posts_from_coordinated_groups": round(coordinated_share, 3),
            "caveat": "First *observed* post. Deleted or uncollected posts may be earlier.",
        })
    return out
