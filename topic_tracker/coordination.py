"""Coordinated-activity detection.

Each signal adds *evidence edges* between pairs of accounts. Edges are combined into one
weighted graph, communities are found with Louvain, and each community is described by the
signals that actually hold inside it. A group is flagged only when at least
`min_signal_types` independent signals agree, because any single signal has innocent
explanations (copypasta, fans, news syndication, default avatars).

Signals
  text_reuse            near-identical text (MinHash LSH over character 5-grams)
  co_posting            same content / link / target posted within a short window, repeatedly
  shared_avatar         same or near-identical profile photo (pHash Hamming distance)
  co_amplification      forwarding/commenting on the same set of posts (Jaccard)
  mutual_amplification  A amplifies B and B amplifies A
  creation_burst        (group level) many members created within a few days
"""
from __future__ import annotations

import logging
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from itertools import combinations

import networkx as nx
from datasketch import MinHash, MinHashLSH

from .images import hamming
from .models import Dataset, iso
from .text import canonical_url, normalize, shingles

log = logging.getLogger(__name__)

TEXT_REUSE = "text_reuse"
CO_POSTING = "co_posting"
SHARED_AVATAR = "shared_avatar"
CO_AMPLIFICATION = "co_amplification"
MUTUAL_AMPLIFICATION = "mutual_amplification"
CREATION_BURST = "creation_burst"


@dataclass
class CoordinationConfig:
    dup_threshold: float = 0.8          # MinHash Jaccard for "near-identical"
    min_text_len: int = 40              # shorter texts ("great video!") are too generic
    num_perm: int = 128
    copost_window_s: int = 300          # "within minutes"
    min_copost_events: int = 3          # must happen on this many separate occasions
    max_key_posts: int = 500            # ignore keys so common they say nothing
    avatar_max_distance: int = 6        # pHash bits
    max_avatar_reuse: int = 50          # exact hash used by more accounts => default avatar
    coamp_min_shared: int = 3
    coamp_min_jaccard: float = 0.5
    max_target_amplifiers: int = 200    # mega-viral targets are not informative
    creation_window_days: int = 7
    creation_min_share: float = 0.5
    max_pairwise_accounts: int = 300
    min_group_size: int = 3
    min_signal_types: int = 2
    min_signal_coverage: float = 0.3    # a signal must involve >= 30% of members to count
    min_member_weight: float = 1.0
    resolution: float = 1.0
    seed: int = 42
    edge_norm: dict = field(default_factory=lambda: {
        TEXT_REUSE: 2, CO_POSTING: 3, SHARED_AVATAR: 1, CO_AMPLIFICATION: 3, MUTUAL_AMPLIFICATION: 2})


class _UnionFind:
    def __init__(self):
        self.parent: dict = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        self.parent[self.find(a)] = self.find(b)


class _Edges:
    def __init__(self):
        self.data: dict = defaultdict(dict)

    def add(self, a: str, b: str, signal: str, count: int = 1, example=None, max_examples: int = 3):
        if a == b:
            return
        key = (a, b) if a < b else (b, a)
        e = self.data[key].setdefault(signal, {"count": 0, "examples": []})
        e["count"] += count
        if example is not None and len(e["examples"]) < max_examples:
            e["examples"].append(example)


# ------------------------------------------------------------------ duplicates
def find_duplicate_clusters(ds: Dataset, cfg: CoordinationConfig) -> list[list[str]]:
    """Groups of near-identical posts (post ids, oldest first). Forwards are excluded:
    they are identical by design and are handled as amplification instead."""
    lsh = MinHashLSH(threshold=cfg.dup_threshold, num_perm=cfg.num_perm)
    hashes = {}
    for p in ds.posts.values():
        if p.share_of or len(normalize(p.text)) < cfg.min_text_len:
            continue
        m = MinHash(num_perm=cfg.num_perm)
        for s in shingles(p.text):
            m.update(s.encode("utf-8"))
        hashes[p.id] = m
        lsh.insert(p.id, m)
    uf = _UnionFind()
    for pid, m in hashes.items():
        uf.find(pid)
        for other in lsh.query(m):
            if other != pid:
                uf.union(pid, other)
    groups = defaultdict(list)
    for pid in hashes:
        groups[uf.find(pid)].append(pid)
    clusters = [sorted(g, key=lambda i: ds.posts[i].created_at) for g in groups.values() if len(g) > 1]
    clusters.sort(key=lambda g: ds.posts[g[0]].created_at)
    log.info("Found %d near-duplicate clusters", len(clusters))
    return clusters


# ------------------------------------------------------------------ signals
def _text_reuse(ds, clusters, edges, cfg):
    for pids in clusters:
        first_by_account = {}
        for pid in pids:
            first_by_account.setdefault(ds.posts[pid].author_id, pid)
        accounts = list(first_by_account)[: cfg.max_pairwise_accounts]
        for a, b in combinations(accounts, 2):
            edges.add(a, b, TEXT_REUSE, 1, {"posts": [first_by_account[a], first_by_account[b]]})


def _co_posting(ds, clusters, edges, cfg):
    """Pairs who post the same thing (duplicate text, same link, same forwarded post, same
    comment target) within the window. Counted per distinct *moment*, so one burst counts once."""
    keys = defaultdict(set)
    for i, pids in enumerate(clusters):
        keys[f"dup:{i}"].update(pids)
    for p in ds.posts.values():
        for u in p.urls:
            keys["url:" + canonical_url(u)].add(p.id)
        if p.share_of:
            keys["share:" + p.share_of].add(p.id)
        if p.reply_to:
            keys["reply:" + p.reply_to].add(p.id)

    window = cfg.copost_window_s
    moments: dict = defaultdict(dict)
    for key, pids in keys.items():
        if not 2 <= len(pids) <= cfg.max_key_posts:
            continue
        ps = sorted((ds.posts[i] for i in pids), key=lambda p: p.created_at)
        ts = [p.created_at.timestamp() for p in ps]
        for i in range(len(ps)):
            j = i + 1
            while j < len(ps) and ts[j] - ts[i] <= window:
                a, b = ps[i].author_id, ps[j].author_id
                if a != b:
                    pair = (a, b) if a < b else (b, a)
                    moments[pair].setdefault(int(ts[i] // window), {
                        "posts": [ps[i].id, ps[j].id], "gap_s": int(ts[j] - ts[i])})
                j += 1
    for (a, b), ms in moments.items():
        if len(ms) >= cfg.min_copost_events:
            examples = [ms[k] for k in sorted(ms)][:3]
            edges.add(a, b, CO_POSTING, len(ms))
            edges.data[(a, b)][CO_POSTING]["examples"] = examples


def _shared_avatars(ds, edges, cfg, only: set | None = None):
    accounts = [a for a in ds.accounts.values() if a.avatar_phash and (only is None or a.id in only)]
    exact = Counter(a.avatar_phash for a in accounts)
    accounts = [a for a in accounts if exact[a.avatar_phash] <= cfg.max_avatar_reuse]
    # 8 bands of 8 bits: any pair within 7 bits shares at least one band (pigeonhole).
    bands = defaultdict(list)
    for a in accounts:
        h = a.avatar_phash.zfill(16)
        for b in range(8):
            bands[(b, h[2 * b:2 * b + 2])].append(a)
    checked = set()
    for members in bands.values():
        if len(members) < 2 or len(members) > 2000:
            continue
        for a, b in combinations(members, 2):
            pair = (a.id, b.id) if a.id < b.id else (b.id, a.id)
            if pair in checked:
                continue
            checked.add(pair)
            d = hamming(a.avatar_phash, b.avatar_phash)
            if d <= cfg.avatar_max_distance:
                edges.add(a.id, b.id, SHARED_AVATAR, 1, {"hamming_distance": d})


def _amplification(ds, edges, cfg):
    targets = defaultdict(set)
    directed = Counter()
    for p in ds.posts.values():
        for target, target_author in ((p.share_of, p.share_of_author), (p.reply_to, None)):
            if not target:
                continue
            if target_author is None and target in ds.posts:
                target_author = ds.posts[target].author_id
            targets[p.author_id].add(target)
            if target_author and target_author != p.author_id:
                directed[(p.author_id, target_author)] += 1

    for (a, b), n in directed.items():
        if a < b and (b, a) in directed:
            edges.add(a, b, MUTUAL_AMPLIFICATION, min(n, directed[(b, a)]),
                      {"a": a, "b": b, "a_to_b": n, "b_to_a": directed[(b, a)]})

    df = Counter(t for ts in targets.values() for t in ts)
    inverted = defaultdict(list)
    for acc, ts in targets.items():
        for t in ts:
            if df[t] <= cfg.max_target_amplifiers:
                inverted[t].append(acc)
    shared = Counter()
    for accs in inverted.values():
        for a, b in combinations(sorted(accs), 2):
            shared[(a, b)] += 1
    for (a, b), n in shared.items():
        if n < cfg.coamp_min_shared:
            continue
        jac = n / len(targets[a] | targets[b])
        if jac >= cfg.coamp_min_jaccard:
            common = sorted(targets[a] & targets[b])[:3]
            edges.add(a, b, CO_AMPLIFICATION, n, {"shared_targets": n, "jaccard": round(jac, 2),
                                                   "targets": common})


def _creation_burst(ds, members, cfg):
    dated = sorted((ds.accounts[m].created_at, m) for m in members
                   if m in ds.accounts and ds.accounts[m].created_at)
    if len(dated) < 3:
        return None
    best, best_span, j = 0, (0, 0), 0
    for i in range(len(dated)):
        while (dated[i][0] - dated[j][0]).days > cfg.creation_window_days:
            j += 1
        if i - j + 1 > best:
            best, best_span = i - j + 1, (j, i)
    if best < 3 or best < cfg.creation_min_share * len(dated):
        return None
    start, end = dated[best_span[0]][0], dated[best_span[1]][0]
    return {
        "signal": CREATION_BURST,
        "summary": (f"{best} of {len(dated)} accounts with a known creation date were created within "
                    f"{cfg.creation_window_days} days ({start:%Y-%m-%d} to {end:%Y-%m-%d})"),
        "accounts_involved": best,
        "evidence": [{"account": ds.account_label(m), "created_at": iso(d)}
                     for d, m in dated[best_span[0]:best_span[1] + 1]][:10],
    }


# ------------------------------------------------------------------ reasons
def _post_ref(ds, pid):
    p = ds.posts.get(pid)
    if not p:
        return {"post_id": pid}
    return {"post_id": pid, "url": p.url, "account": ds.account_label(p.author_id),
            "time": iso(p.created_at), "text": p.text[:160]}


def _reason(ds, signal, edge_list, involved, cfg):
    n_pairs = len(edge_list)
    counts = [s["count"] for _, _, s in edge_list]
    examples = [ex for _, _, s in edge_list for ex in s["examples"]][:4]
    if signal == TEXT_REUSE:
        evidence = [_post_ref(ds, pid) for ex in examples for pid in ex["posts"]][:6]
        snippet = evidence[0]["text"][:90] if evidence else ""
        most = max(counts)
        summary = (f"{len(involved)} accounts posted near-identical text ({n_pairs} account pairs; up to {most} "
                   f"distinct message{'s' if most > 1 else ''} duplicated per pair), e.g. \"{snippet}...\"")
    elif signal == CO_POSTING:
        evidence = [dict(_post_ref(ds, ex["posts"][0]), paired_with=_post_ref(ds, ex["posts"][1]),
                         gap_seconds=ex["gap_s"]) for ex in examples]
        summary = (f"{len(involved)} accounts posted the same content within {cfg.copost_window_s // 60} "
                   f"minutes of each other, repeatedly (up to {max(counts)} separate occasions per pair)")
    elif signal == SHARED_AVATAR:
        evidence = [{"accounts": [ds.account_label(a), ds.account_label(b)], **s["examples"][0]}
                    for a, b, s in edge_list][:6]
        summary = f"{len(involved)} accounts use the same or a near-identical profile photo"
    elif signal == CO_AMPLIFICATION:
        evidence = [{"accounts": [ds.account_label(a), ds.account_label(b)],
                     "shared_targets": s["examples"][0]["shared_targets"],
                     "jaccard": s["examples"][0]["jaccard"],
                     "targets": [_post_ref(ds, t).get("url", t) for t in s["examples"][0]["targets"]]}
                    for a, b, s in edge_list][:5]
        summary = (f"{len(involved)} accounts repeatedly amplified the same posts "
                   f"(forwarded or commented on up to {max(counts)} of the same targets)")
    else:  # MUTUAL_AMPLIFICATION
        evidence = [{"accounts": [ds.account_label(ex["a"]), ds.account_label(ex["b"])],
                     "a_to_b": ex["a_to_b"], "b_to_a": ex["b_to_a"]} for ex in examples]
        summary = f"{n_pairs} pairs of accounts amplify each other (A forwards/replies to B and B to A)"
    return {"signal": signal, "summary": summary, "accounts_involved": len(involved),
            "pairs": n_pairs, "evidence": evidence}


def _describe(ds, sub: nx.Graph, members: list[str], cfg) -> dict:
    per_signal = defaultdict(list)
    for a, b, d in sub.edges(data=True):
        for name, s in d["signals"].items():
            per_signal[name].append((a, b, s))
    reasons = []
    for name, edge_list in per_signal.items():
        involved = {x for a, b, _ in edge_list for x in (a, b)}
        needed = 2 if name == SHARED_AVATAR else max(3, math.ceil(cfg.min_signal_coverage * len(members)))
        if len(involved) >= needed:
            reasons.append(_reason(ds, name, edge_list, involved, cfg))
    burst = _creation_burst(ds, members, cfg)
    if burst:
        reasons.append(burst)
    reasons.sort(key=lambda r: -r["accounts_involved"])
    n_types = len(reasons)
    strength = {m: round(sub.degree(m, weight="weight"), 2) for m in members}
    return {
        "size": len(members),
        "confidence": "high" if n_types >= 3 else "medium" if n_types == 2 else "low",
        "flagged": n_types >= cfg.min_signal_types,
        "signal_types": [r["signal"] for r in reasons],
        "score": round(sum(d["weight"] for _, _, d in sub.edges(data=True)) / len(members), 2),
        "platforms": dict(Counter(m.split(":")[0] for m in members)),
        "reasons": reasons,
        "members": sorted(({
            "id": m, "label": ds.account_label(m),
            "url": ds.accounts[m].url if m in ds.accounts else None,
            "created_at": iso(ds.accounts[m].created_at) if m in ds.accounts else None,
            "evidence_strength": strength[m]} for m in members), key=lambda x: -x["evidence_strength"]),
    }


def detect_coordination(ds: Dataset, clusters: list[list[str]], cfg: CoordinationConfig,
                        avatar_fetcher=None) -> list[dict]:
    """`avatar_fetcher(ds, account_ids)` downloads profile photos on demand. Photos are only compared
    between accounts already linked by another signal: a group needs two independent signals to be
    flagged, so a photo match on its own could never flag anyone, and this avoids downloading
    thousands of photos that cannot change the result."""
    edges = _Edges()
    _text_reuse(ds, clusters, edges, cfg)
    _co_posting(ds, clusters, edges, cfg)
    _amplification(ds, edges, cfg)
    linked = {x for pair in edges.data for x in pair}
    if avatar_fetcher is not None and linked:
        avatar_fetcher(ds, linked)
    _shared_avatars(ds, edges, cfg, only=linked)

    g = nx.Graph()
    for (a, b), signals in edges.data.items():
        w = sum(min(1.0, s["count"] / cfg.edge_norm[name]) for name, s in signals.items())
        g.add_edge(a, b, weight=w, signals=signals)
    log.info("Coordination graph: %d accounts, %d edges", g.number_of_nodes(), g.number_of_edges())
    if g.number_of_edges() == 0:
        return []

    groups = []
    for community in nx.community.louvain_communities(g, weight="weight", seed=cfg.seed,
                                                      resolution=cfg.resolution):
        if len(community) < cfg.min_group_size:
            continue
        sub = g.subgraph(community)
        members = [m for m in community if sub.degree(m, weight="weight") >= cfg.min_member_weight]
        if len(members) < cfg.min_group_size:
            continue
        groups.append(_describe(ds, g.subgraph(members), members, cfg))

    rank = {"high": 2, "medium": 1, "low": 0}
    groups.sort(key=lambda x: (x["flagged"], rank[x["confidence"]], x["score"]), reverse=True)
    for i, grp in enumerate(groups, 1):
        grp["id"] = f"G{i}"
    return groups
