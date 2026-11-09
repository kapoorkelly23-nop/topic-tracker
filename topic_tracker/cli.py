"""Command line.

  python -m topic_tracker run      --topic "#AcmeBankCollapse" --since 2026-10-01 --until 2026-10-31 --out runs/acme
  python -m topic_tracker collect  ... (collect only)
  python -m topic_tracker analyze  --data runs/acme
  python -m topic_tracker refresh  --data runs/acme   (update engagement numbers, rebuild outputs)
  python -m topic_tracker demo     --out runs/demo    (synthetic data + scoring, no API keys needed)
  python -m topic_tracker serve                       (search page in the browser: http://127.0.0.1:8765)
  python -m topic_tracker telegram-login              (one-time Telegram login, needed before using the page)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, time, timezone
from pathlib import Path

from .coordination import CoordinationConfig
from .models import Dataset
from .narratives import NarrativeConfig
from .text import TopicQuery


def _date(s: str, end: bool = False) -> datetime:
    d = datetime.fromisoformat(s)
    if len(s) == 10:
        d = datetime.combine(d.date(), time.max if end else time.min)
    return d.replace(tzinfo=d.tzinfo or timezone.utc)


def _channels(value: str | None) -> list[str]:
    if not value:
        return []
    p = Path(value)
    if p.exists():
        return [line.strip() for line in p.read_text().splitlines() if line.strip() and not line.startswith("#")]
    return [c.strip() for c in value.split(",") if c.strip()]


def _connectors(args):
    from .connectors import TelegramConnector, YouTubeConnector

    out = []
    if "youtube" in args.platforms:
        key = os.getenv("YOUTUBE_API_KEY")
        if not key:
            sys.exit("YOUTUBE_API_KEY is not set (see README)")
        out.append(YouTubeConnector(key, max_videos=args.yt_max_videos, max_search_calls=args.yt_max_search_calls,
                                    include_comments=not args.yt_no_comments,
                                    max_comments_per_video=args.yt_max_comments))
    if "telegram" in args.platforms:
        api_id, api_hash = os.getenv("TELEGRAM_API_ID"), os.getenv("TELEGRAM_API_HASH")
        if not (api_id and api_hash):
            sys.exit("TELEGRAM_API_ID / TELEGRAM_API_HASH are not set (see README)")
        out.append(TelegramConnector(api_id, api_hash, session=args.tg_session, channels=_channels(args.tg_channels),
                                     discover=not args.tg_no_discover, snowball_depth=args.tg_snowball_depth,
                                     max_chats=args.tg_max_chats, download_photos=args.tg_download_photos))
    return out


def _analysis_configs(args):
    ncfg = NarrativeConfig(embedding_model=args.embeddings, min_cluster_size=args.min_cluster_size)
    ccfg = CoordinationConfig(copost_window_s=args.copost_window * 60, min_group_size=args.min_group_size,
                              min_signal_types=args.min_signal_types)
    return ncfg, ccfg


def main(argv=None):
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    ap = argparse.ArgumentParser(prog="topic_tracker", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def collect_args(p):
        p.add_argument("--topic", required=True, help='keyword, "#hashtag", person, brand or event')
        p.add_argument("--since", required=True, help="YYYY-MM-DD (UTC)")
        p.add_argument("--until", required=True, help="YYYY-MM-DD (UTC, inclusive)")
        p.add_argument("--out", required=True, help="output directory")
        p.add_argument("--platforms", nargs="+", default=["youtube", "telegram"], choices=["youtube", "telegram"])
        p.add_argument("--yt-max-videos", type=int, default=300)
        p.add_argument("--yt-max-search-calls", type=int, default=40, help="each costs 100 of 10,000 daily units")
        p.add_argument("--yt-max-comments", type=int, default=300, help="per video")
        p.add_argument("--yt-no-comments", action="store_true")
        p.add_argument("--tg-channels", help="comma list or file of usernames / t.me links to seed")
        p.add_argument("--tg-no-discover", action="store_true", help="don't search channel names for the topic")
        p.add_argument("--tg-snowball-depth", type=int, default=1)
        p.add_argument("--tg-max-chats", type=int, default=60)
        p.add_argument("--tg-download-photos", action="store_true", help="hash post photos (slower)")
        p.add_argument("--tg-session", default="topic_tracker")

    def analysis_args(p):
        p.add_argument("--embeddings", nargs="?", const="paraphrase-multilingual-MiniLM-L12-v2", default=None,
                       help="use a sentence-transformers model instead of TF-IDF (for multilingual data)")
        p.add_argument("--min-cluster-size", type=int, default=None)
        p.add_argument("--copost-window", type=int, default=5, help="minutes")
        p.add_argument("--min-group-size", type=int, default=3)
        p.add_argument("--min-signal-types", type=int, default=2)
        p.add_argument("--no-avatars", action="store_true", help="skip profile photo checks")

    collect_args(sub.add_parser("collect", help="collect posts only"))
    p_run = sub.add_parser("run", help="collect then analyse")
    collect_args(p_run)
    analysis_args(p_run)
    p_an = sub.add_parser("analyze", help="analyse a collected dataset")
    p_an.add_argument("--data", required=True)
    p_an.add_argument("--out", help="defaults to <data>/analysis")
    analysis_args(p_an)
    p_ref = sub.add_parser("refresh", help="re-fetch current engagement for collected posts, then re-analyse")
    p_ref.add_argument("--data", required=True)
    p_ref.add_argument("--platforms", nargs="+", default=None, choices=["youtube", "telegram"])
    p_ref.add_argument("--tg-session", default="topic_tracker")
    analysis_args(p_ref)
    p_serve = sub.add_parser("serve", help="open the search page in your browser")
    p_serve.add_argument("--port", type=int, default=8765)
    p_serve.add_argument("--runs", default="runs", help="folder where searches are saved")
    p_serve.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    p_serve.add_argument("--tg-session", default="topic_tracker")
    p_login = sub.add_parser("telegram-login", help="log in to Telegram once (asks for phone and code)")
    p_login.add_argument("--tg-session", default="topic_tracker")
    p_demo = sub.add_parser("demo", help="synthetic dataset with planted networks, analysed and scored")
    p_demo.add_argument("--out", default="runs/demo")
    p_demo.add_argument("--seed", type=int, default=7)
    analysis_args(p_demo)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)

    from . import pipeline

    if args.cmd in ("collect", "run"):
        query = TopicQuery(args.topic, _date(args.since), _date(args.until, end=True))
        ds = pipeline.collect(query, _connectors(args), args.out)
        if args.cmd == "run":
            ncfg, ccfg = _analysis_configs(args)
            pipeline.analyze(ds, Path(args.out) / "analysis", ncfg, ccfg, check_avatars=not args.no_avatars)
    elif args.cmd == "analyze":
        ncfg, ccfg = _analysis_configs(args)
        pipeline.analyze(Dataset.load(args.data), args.out or Path(args.data) / "analysis", ncfg, ccfg,
                         check_avatars=not args.no_avatars)
    elif args.cmd == "serve":
        from .server import serve
        serve(args.runs, port=args.port, open_browser=not args.no_browser, session=args.tg_session)
    elif args.cmd == "telegram-login":
        from .connectors import TelegramConnector
        api_id, api_hash = os.getenv("TELEGRAM_API_ID"), os.getenv("TELEGRAM_API_HASH")
        if not (api_id and api_hash):
            sys.exit("TELEGRAM_API_ID / TELEGRAM_API_HASH are not set in .env (see README)")
        name = TelegramConnector(api_id, api_hash, session=args.tg_session).login()
        print(f"Logged in to Telegram as {name}. You can now use Telegram in the search page.")
    elif args.cmd == "refresh":
        from .connectors import TelegramConnector, YouTubeConnector
        ds = Dataset.load(args.data)
        platforms = args.platforms or ds.meta.get("platforms") or ["youtube", "telegram"]
        connectors = []
        if "youtube" in platforms and os.getenv("YOUTUBE_API_KEY"):
            connectors.append(YouTubeConnector(os.environ["YOUTUBE_API_KEY"]))
        if "telegram" in platforms and os.getenv("TELEGRAM_API_ID") and os.getenv("TELEGRAM_API_HASH"):
            connectors.append(TelegramConnector(os.environ["TELEGRAM_API_ID"], os.environ["TELEGRAM_API_HASH"],
                                                session=args.tg_session))
        if not connectors:
            sys.exit("No API keys found for the platforms to refresh (see README)")
        pipeline.refresh(ds, connectors, args.data)
        ncfg, ccfg = _analysis_configs(args)
        pipeline.analyze(ds, Path(args.data) / "analysis", ncfg, ccfg, check_avatars=not args.no_avatars)
    elif args.cmd == "demo":
        from . import synthetic
        ds = synthetic.write(args.out, args.seed)
        ncfg, ccfg = _analysis_configs(args)
        results = pipeline.analyze(ds, Path(args.out) / "analysis", ncfg, ccfg, check_avatars=not args.no_avatars)
        truth = json.loads((Path(args.out) / "ground_truth.json").read_text())
        score = synthetic.evaluate(results, truth)
        (Path(args.out) / "analysis" / "evaluation.json").write_text(json.dumps(score, indent=2))
        print(json.dumps(score, indent=2))


if __name__ == "__main__":
    main()
