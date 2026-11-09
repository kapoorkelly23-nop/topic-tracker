"""Narrative discovery: what is being said, grouped, with volume over time per platform.

Pipeline: dedupe identical texts -> embed -> (optional UMAP) -> HDBSCAN -> attach outliers to the
nearest narrative if similar enough -> label with class-based TF-IDF terms + representative posts.

Topic words (e.g. "Acme", "Bank", "#AcmeBankCollapse") are removed before vectorising: every post
mentions them, so they would make all posts look alike and hide what is actually being said.

Vectoriser: TF-IDF + SVD by default (no heavy dependencies; separated all planted narratives in the
synthetic test). For mixed-language data, pass embedding_model to use a multilingual
sentence-transformers model instead (pip install sentence-transformers).
"""
from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Optional

import numpy as np
from sklearn.cluster import HDBSCAN
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS, CountVectorizer, TfidfVectorizer
from sklearn.preprocessing import normalize as l2_normalize

from .models import Dataset, Post, iso
from .text import normalize

log = logging.getLogger(__name__)


@dataclass
class NarrativeConfig:
    embedding_model: Optional[str] = None    # e.g. "paraphrase-multilingual-MiniLM-L12-v2"
    min_text_len: int = 20
    min_cluster_size: Optional[int] = None   # None => max(5, 1% of unique texts)
    min_samples: Optional[int] = None
    outlier_similarity: Optional[float] = None  # attach outliers above this cosine (None: 0.5 embeddings / 0.3 TF-IDF)
    use_umap: bool = False                   # optional dimensionality reduction (pip install umap-learn)
    merge_similarity: Optional[float] = None # merge narratives with closer centroids (None: 0.85 / 0.5)
    top_terms: int = 8
    n_representative: int = 3


def _doc(p: Post) -> str:
    return (p.text or "")[:1500]


def embed(docs: list[str], cfg: NarrativeConfig) -> tuple[np.ndarray, str]:
    """Returns (unit-length vectors, method)."""
    if cfg.embedding_model:
        try:
            from sentence_transformers import SentenceTransformer
            model = SentenceTransformer(cfg.embedding_model)
            return np.asarray(model.encode(docs, batch_size=64, normalize_embeddings=True,
                                           show_progress_bar=False)), "embeddings"
        except ImportError:
            log.info("sentence-transformers not installed; falling back to TF-IDF + SVD")
    vec = TfidfVectorizer(ngram_range=(1, 2), min_df=2 if len(docs) >= 50 else 1, sublinear_tf=True,
                          max_features=50000, token_pattern=r"(?u)\b\w\w+\b")
    x = vec.fit_transform(docs)
    k = min(100, x.shape[1] - 1, x.shape[0] - 1)
    if k >= 2:
        x = TruncatedSVD(n_components=k, random_state=0).fit_transform(x)
    else:
        x = x.toarray()
    return l2_normalize(x), "tfidf"


def _reduce(x: np.ndarray, cfg: NarrativeConfig) -> np.ndarray:
    if not cfg.use_umap or len(x) < 50:
        return x
    try:
        import umap
    except ImportError:
        return x
    return umap.UMAP(n_components=10, metric="cosine", random_state=0).fit_transform(x)


def _strip_terms(text: str, terms: set[str]) -> str:
    if not terms:
        return text
    pattern = r"#?\b(" + "|".join(sorted(map(re.escape, terms), key=len, reverse=True)) + r")\b"
    return re.sub(pattern, " ", text, flags=re.I)


def _merge_close(x: np.ndarray, labels: np.ndarray, threshold: float) -> np.ndarray:
    """Greedy average-linkage merge of clusters whose centroids are very similar (over-splitting)."""
    while True:
        ids = sorted(set(labels) - {-1})
        if len(ids) < 2:
            return labels
        cents = l2_normalize(np.vstack([x[labels == c].mean(axis=0) for c in ids]))
        sims = cents @ cents.T
        np.fill_diagonal(sims, -1)
        i, j = np.unravel_index(sims.argmax(), sims.shape)
        if sims[i, j] < threshold:
            return labels
        labels[labels == ids[j]] = ids[i]


def _ctfidf_terms(class_docs: list[str], exclude: set[str], top_n: int) -> list[list[str]]:
    """Class-based TF-IDF (as in BERTopic): words frequent in one narrative, rare in others."""
    stop = list(ENGLISH_STOP_WORDS | exclude | {"http", "https", "www", "com"})
    cv = CountVectorizer(stop_words=stop, ngram_range=(1, 2), token_pattern=r"(?u)\b[^\W\d_]{3,}\b")
    try:
        counts = cv.fit_transform(class_docs).toarray().astype(float)
    except ValueError:  # empty vocabulary
        return [[] for _ in class_docs]
    vocab = cv.get_feature_names_out()
    tf = counts / np.maximum(counts.sum(axis=1, keepdims=True), 1)
    avg_words = counts.sum() / len(class_docs)
    idf = np.log(1 + avg_words / np.maximum(counts.sum(axis=0), 1))
    scores = tf * idf
    out = []
    for row in scores:
        picked: list[str] = []
        for i in row.argsort()[::-1]:
            if row[i] <= 0 or len(picked) >= top_n:
                break
            term = vocab[i]
            if any(term in p or p in term for p in picked):
                continue
            picked.append(term)
        out.append(picked)
    return out


def build_narratives(ds: Dataset, exclude_terms: set[str], cfg: NarrativeConfig
                     ) -> tuple[list[dict], dict[str, str], dict]:
    """Returns (narratives, post_id -> narrative_id, summary of unclustered posts)."""
    posts = [p for p in ds.posts.values() if len(normalize(_doc(p))) >= cfg.min_text_len]
    by_text: dict[str, list[Post]] = defaultdict(list)
    for p in sorted(posts, key=lambda p: p.created_at):
        by_text[normalize(_doc(p))].append(p)
    keys = list(by_text)
    if not keys:
        return [], {}, {"posts": 0}
    docs = [_doc(by_text[k][0]) for k in keys]
    x, method = embed([_strip_terms(d, exclude_terms) for d in docs], cfg)
    threshold = cfg.outlier_similarity or (0.5 if method == "embeddings" else 0.3)
    merge_at = cfg.merge_similarity or (0.85 if method == "embeddings" else 0.5)

    mcs = cfg.min_cluster_size or max(5, len(keys) // 100)
    if len(keys) < 2 * mcs:
        labels = np.zeros(len(keys), dtype=int)
    else:
        labels = HDBSCAN(min_cluster_size=mcs, min_samples=cfg.min_samples, copy=True).fit_predict(_reduce(x, cfg))
        labels = _merge_close(x, labels, merge_at)

    cluster_ids = sorted(set(labels) - {-1})
    if cluster_ids:  # attach outliers to the closest narrative centroid
        centroids = l2_normalize(np.vstack([x[labels == c].mean(axis=0) for c in cluster_ids]))
        noise = np.where(labels == -1)[0]
        if len(noise):
            sims = x[noise] @ centroids.T
            best = sims.argmax(axis=1)
            for k, (row, b) in enumerate(zip(noise, best)):
                if sims[k, b] >= threshold:
                    labels[row] = cluster_ids[b]
    # A message repeated many times collapses to one point after dedupe, so density clustering
    # cannot see it. If it is still unassigned, it is a narrative in its own right.
    next_id = max(cluster_ids, default=-1) + 1
    for i in np.where(labels == -1)[0]:
        if len(by_text[keys[i]]) >= mcs:
            labels[i] = next_id
            next_id += 1

    members: dict[int, list[int]] = defaultdict(list)
    for i, lab in enumerate(labels):
        members[int(lab)].append(i)
    class_ids = [c for c in members if c != -1]
    terms = _ctfidf_terms([" ".join(docs[i] for i in members[c]) for c in class_ids],
                          {t.lower() for t in exclude_terms}, cfg.top_terms)

    narratives, assignment = [], {}
    for c, top in zip(class_ids, terms):
        idx = members[c]
        cluster_posts = [p for i in idx for p in by_text[keys[i]]]
        centroid = l2_normalize(x[idx].mean(axis=0, keepdims=True))[0]
        rep_idx = [idx[j] for j in np.argsort(-(x[idx] @ centroid))[: cfg.n_representative]]
        narratives.append(_summarise(ds, cluster_posts, top, [by_text[keys[i]][0] for i in rep_idx]))

    narratives.sort(key=lambda n: -n["posts"])
    for i, n in enumerate(narratives, 1):
        n["id"] = f"N{i}"
        for pid in n["post_ids"]:
            assignment[pid] = n["id"]
    unclustered = [p for i in members.get(-1, []) for p in by_text[keys[i]]]
    return narratives, assignment, {"posts": len(unclustered),
                                    "platforms": dict(Counter(p.platform for p in unclustered))}


def _summarise(ds: Dataset, posts: list[Post], terms: list[str], reps: list[Post]) -> dict:
    posts = sorted(posts, key=lambda p: p.created_at)
    volume: dict[str, Counter] = defaultdict(Counter)
    for p in posts:
        volume[p.created_at.strftime("%Y-%m-%d")][p.platform] += 1
    daily_totals = {d: sum(c.values()) for d, c in volume.items()}
    peak = max(daily_totals, key=daily_totals.get)
    return {
        "label": ", ".join(terms[:3]) if terms else "(no distinctive terms)",
        "top_terms": terms,
        "posts": len(posts),
        "accounts": len({p.author_id for p in posts}),
        "platforms": dict(Counter(p.platform for p in posts)),
        "engagement": sum(p.engagement.score() for p in posts),
        "first_seen": iso(posts[0].created_at),
        "last_seen": iso(posts[-1].created_at),
        "peak_day": peak,
        "peak_day_posts": daily_totals[peak],
        "representative_posts": [{"post_id": p.id, "url": p.url, "platform": p.platform,
                                  "account": ds.account_label(p.author_id), "text": p.text[:280]}
                                 for p in reps],
        "volume": {d: dict(c) for d, c in sorted(volume.items())},
        "post_ids": [p.id for p in posts],
    }
