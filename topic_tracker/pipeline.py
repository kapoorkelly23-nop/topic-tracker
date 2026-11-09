"""Glue: collect -> analyse -> report."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .connectors.base import Connector
from .coordination import CoordinationConfig, detect_coordination, find_duplicate_clusters
from .images import enrich_avatars
from .models import Dataset, iso
from .narratives import NarrativeConfig, build_narratives
from .origins import OriginConfig, trace_origins
from .report import write_report
from .text import TopicQuery

log = logging.getLogger(__name__)


def collect(query: TopicQuery, connectors: Iterable[Connector], out_dir: str | Path) -> Dataset:
    ds = Dataset()
    ds.meta = {"topic": query.topic, "since": iso(query.since), "until": iso(query.until), "platforms": [],
               "collected_at": iso(datetime.now(timezone.utc))}
    for c in connectors:
        log.info("Collecting from %s", c.platform)
        try:
            c.collect(query, ds)
            ds.meta["platforms"].append(c.platform)
        except Exception:  # one platform failing should not lose the others
            log.exception("%s collection failed", c.platform)
        ds.save(out_dir)   # checkpoint after each platform
    log.info("Saved %d posts, %d accounts to %s", len(ds.posts), len(ds.accounts), out_dir)
    return ds


def analyze(ds: Dataset, out_dir: str | Path, ncfg: NarrativeConfig | None = None,
            ccfg: CoordinationConfig | None = None, ocfg: OriginConfig | None = None,
            check_avatars: bool = True) -> dict:
    ncfg, ccfg, ocfg = ncfg or NarrativeConfig(), ccfg or CoordinationConfig(), ocfg or OriginConfig()
    topic = ds.meta.get("topic", "")
    exclude = TopicQuery(topic, None, None).terms() if topic else set()

    clusters = find_duplicate_clusters(ds, ccfg)
    narratives, assignment, unclustered = build_narratives(ds, exclude, ncfg)
    log.info("Found %d narratives", len(narratives))
    groups = detect_coordination(ds, clusters, ccfg,
                                 avatar_fetcher=(lambda d, ids: enrich_avatars(d, ids)) if check_avatars else None)
    log.info("Coordination: %d candidate groups, %d flagged", len(groups), sum(g["flagged"] for g in groups))
    origins = trace_origins(ds, narratives, clusters, groups, ocfg)

    results = {"narratives": narratives, "assignment": assignment, "unclustered": unclustered,
               "groups": groups, "origins": origins, "duplicate_clusters": len(clusters)}
    report = write_report(ds, results, out_dir)
    log.info("Report written to %s", report)
    return results


def refresh(ds: Dataset, connectors: Iterable[Connector], data_dir: str | Path) -> Dataset:
    """Update engagement numbers of already-collected posts and mark posts that have disappeared."""
    now = datetime.now(timezone.utc)
    for c in connectors:
        log.info("Refreshing %s engagement", c.platform)
        try:
            c.refresh(ds, now)
        except Exception:
            log.exception("%s refresh failed", c.platform)
    ds.meta["refreshed_at"] = iso(now)
    ds.save(data_dir)
    gone = sum(1 for p in ds.posts.values() if not p.available)
    log.info("Refreshed engagement; %d posts no longer available", gone)
    return ds
