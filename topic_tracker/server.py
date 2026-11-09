"""Local search page: type a topic, pick dates, click Search, see the dashboard.

    python -m topic_tracker serve        then open http://127.0.0.1:8765

Uses only the Python standard library (no extra installs). It listens on 127.0.0.1, so only
this machine can reach it, and API keys stay in .env on the server side, never in the page.
One search runs at a time, to protect the YouTube quota.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional

from . import pipeline
from .models import Dataset, iso
from .connectors.youtube import redact
from .text import TopicQuery

log = logging.getLogger(__name__)

DEPTHS = {
    "quick":    {"yt_calls": 4,  "yt_videos": 200,  "yt_comments": 50,  "tg_chats": 15, "tg_messages": 500},
    "standard": {"yt_calls": 10, "yt_videos": 500,  "yt_comments": 300, "tg_chats": 40, "tg_messages": 2000},
    "deep":     {"yt_calls": 40, "yt_videos": 2000, "yt_comments": 300, "tg_chats": 80, "tg_messages": 5000},
}
SEARCH_STAGES = ["Collecting posts", "Finding narratives", "Looking for coordinated accounts",
                 "Building the dashboard"]
REFRESH_STAGES = ["Fetching current engagement", "Re-analysing", "Building the dashboard"]
YT_DAILY_UNITS = 10_000


# ------------------------------------------------------------------ helpers
def pacific_day() -> str:
    """YouTube quota resets at midnight Pacific time."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    except Exception:  # noqa: BLE001 - no tz database: assume PST
        return (datetime.now(timezone.utc) - timedelta(hours=8)).date().isoformat()


def next_reset_utc() -> str:
    """Next midnight US Pacific time, as a UTC timestamp the browser shows in local time."""
    try:
        from zoneinfo import ZoneInfo
        pt = ZoneInfo("America/Los_Angeles")
        now = datetime.now(pt)
        midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=pt)
        return midnight.astimezone(timezone.utc).isoformat()
    except Exception:  # noqa: BLE001
        now = datetime.now(timezone.utc) - timedelta(hours=8)
        nxt = datetime.combine(now.date() + timedelta(days=1), datetime.min.time())
        return (nxt + timedelta(hours=8)).replace(tzinfo=timezone.utc).isoformat()


def slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return s[:40] or "search"


class Job:
    def __init__(self, kind: str, label: str):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind                 # "search" | "refresh"
        self.label = label
        self.state = "running"           # running | done | error
        self.stage = 0
        self.stages = SEARCH_STAGES if kind == "search" else REFRESH_STAGES
        self.messages: list[str] = []
        self.warnings: list[str] = []
        self.error: Optional[str] = None
        self.run: Optional[str] = None
        self.started = time.time()
        self.finished: Optional[float] = None

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "label": self.label, "state": self.state,
                "stage": self.stage, "stages": self.stages, "messages": self.messages[-8:],
                "warnings": self.warnings[-6:], "error": self.error, "run": self.run,
                "elapsed": int((self.finished or time.time()) - self.started)}

    def observe(self, msg: str) -> None:
        """Move the progress bar along based on the pipeline's own log messages."""
        if self.kind == "search":
            rules = [("Collecting from", 0), ("Saved ", 1), (" narratives", 2), ("Coordination:", 3)]
        else:
            rules = [("Refreshing", 0), ("Refreshed engagement", 1), ("Coordination:", 2)]
        for needle, stage in rules:
            if needle in msg:
                self.stage = max(self.stage, stage)


class _JobLogHandler(logging.Handler):
    def __init__(self, job: Job):
        super().__init__(logging.INFO)
        self.job = job

    def emit(self, record: logging.LogRecord) -> None:
        if not record.name.startswith("topic_tracker") or record.name == __name__:
            return
        msg = record.getMessage()
        if record.exc_info and record.exc_info[1] is not None:
            msg = f"{msg}: {record.exc_info[1]}"
        msg = redact(msg)
        self.job.observe(msg)
        self.job.messages.append(msg)
        if record.levelno >= logging.WARNING and "search budget" not in msg:
            self.job.warnings.append(msg)


# ------------------------------------------------------------------ app state
class App:
    def __init__(self, runs_dir: Path, session: str = "topic_tracker",
                 connector_factory: Optional[Callable] = None):
        self.runs_dir = Path(runs_dir).resolve()
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.session = session
        self.connector_factory = connector_factory or self._connectors
        self.job: Optional[Job] = None
        self.lock = threading.Lock()
        self._summaries: dict[str, tuple[float, dict]] = {}

    # ---- platforms and quota
    def platforms(self) -> dict:
        tg_keys = bool(os.getenv("TELEGRAM_API_ID") and os.getenv("TELEGRAM_API_HASH"))
        return {
            "youtube": {"ready": bool(os.getenv("YOUTUBE_API_KEY")),
                        "note": "" if os.getenv("YOUTUBE_API_KEY") else "Add YOUTUBE_API_KEY to .env"},
            "telegram": {"ready": tg_keys and Path(f"{self.session}.session").exists(),
                         "note": "" if tg_keys and Path(f"{self.session}.session").exists() else
                         ("Run: python -m topic_tracker telegram-login" if tg_keys else
                          "Add TELEGRAM_API_ID and TELEGRAM_API_HASH to .env")},
        }

    def quota(self) -> dict:
        path = self.runs_dir / ".youtube_quota.json"
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        used = data.get("units", 0) if data.get("day") == pacific_day() else 0
        return {"used": used, "limit": YT_DAILY_UNITS, "resets_at": next_reset_utc()}

    def _add_quota(self, units: int) -> None:
        q = self.quota()
        (self.runs_dir / ".youtube_quota.json").write_text(
            json.dumps({"day": pacific_day(), "units": q["used"] + units}))

    # ---- runs
    def _summary(self, run_dir: Path) -> Optional[dict]:
        meta_path = run_dir / "meta.json"
        dash = run_dir / "analysis" / "dashboard.html"
        if not (meta_path.exists() and dash.exists()):
            return None
        mtime = dash.stat().st_mtime
        cached = self._summaries.get(run_dir.name)
        if cached and cached[0] == mtime:
            return cached[1]
        summary_path = run_dir / "analysis" / "summary.json"
        if summary_path.exists():
            s = json.loads(summary_path.read_text())
        else:   # runs made before summary.json existed
            meta = json.loads(meta_path.read_text())
            with open(run_dir / "posts.jsonl", encoding="utf-8") as fh:
                n_posts = sum(1 for _ in fh)
            groups = json.loads((run_dir / "analysis" / "groups.json").read_text())
            narratives = json.loads((run_dir / "analysis" / "narratives.json").read_text())
            s = {**{k: meta.get(k) for k in ("topic", "since", "until", "collected_at", "refreshed_at")},
                 "posts": n_posts, "platforms": {p: None for p in meta.get("platforms", [])},
                 "narratives": len(narratives), "flagged_groups": sum(1 for g in groups if g.get("flagged"))}
        s = {**s, "name": run_dir.name, "updated": mtime}
        self._summaries[run_dir.name] = (mtime, s)
        return s

    def runs(self) -> list[dict]:
        out = [s for d in self.runs_dir.iterdir() if d.is_dir() and (s := self._summary(d))]
        return sorted(out, key=lambda s: s.get("collected_at") or "", reverse=True)

    def state(self) -> dict:
        return {"runs": self.runs(), "job": self.job.to_dict() if self.job else None,
                "platforms": self.platforms(), "quota": self.quota(), "depths": DEPTHS}

    # ---- jobs
    def _connectors(self, platforms: list[str], depth: dict, channels: list[str]) -> list:
        from .connectors import TelegramConnector, YouTubeConnector
        out = []
        if "youtube" in platforms:
            out.append(YouTubeConnector(os.environ["YOUTUBE_API_KEY"], max_videos=depth["yt_videos"],
                                        max_search_calls=depth["yt_calls"],
                                        max_comments_per_video=depth["yt_comments"]))
        if "telegram" in platforms:
            out.append(TelegramConnector(os.environ["TELEGRAM_API_ID"], os.environ["TELEGRAM_API_HASH"],
                                         session=self.session, channels=channels, max_chats=depth["tg_chats"],
                                         max_messages_per_chat=depth["tg_messages"], interactive=False))
        return out

    def _start(self, job: Job, work: Callable[[Job], None]) -> Job:
        with self.lock:
            if self.job and self.job.state == "running":
                raise RuntimeError("Another search is still running. Wait for it to finish.")
            self.job = job

        def target():
            handler = _JobLogHandler(job)
            root = logging.getLogger()
            root.addHandler(handler)
            if root.level > logging.INFO or root.level == logging.NOTSET:
                root.setLevel(logging.INFO)
            try:
                work(job)
                job.stage = len(job.stages)
                job.state = "done"
            except Exception as e:  # noqa: BLE001 - shown to the user
                log.exception("Job failed")
                job.error = redact(str(e) or e.__class__.__name__)
                job.state = "error"
            finally:
                job.finished = time.time()
                root.removeHandler(handler)
        threading.Thread(target=target, daemon=True).start()
        return job

    def start_search(self, params: dict) -> Job:
        topic = str(params.get("topic", "")).strip()
        if not topic or len(topic) > 200:
            raise ValueError("Enter a topic (up to 200 characters).")
        try:
            since = datetime.fromisoformat(params["since"]).replace(tzinfo=timezone.utc)
            until = datetime.fromisoformat(params["until"]).replace(tzinfo=timezone.utc) + timedelta(days=1, seconds=-1)
        except (KeyError, ValueError):
            raise ValueError("Choose a start and end date.") from None
        if since > until:
            raise ValueError("The start date must be before the end date.")
        ready = self.platforms()
        platforms = [p for p in params.get("platforms", []) if p in ready]
        if not platforms:
            raise ValueError("Choose at least one platform.")
        for p in platforms:
            if not ready[p]["ready"]:
                raise ValueError(f"{p.capitalize()} isn't set up yet: {ready[p]['note']}")
        depth = DEPTHS.get(params.get("depth", "standard"), DEPTHS["standard"])
        channels = [c.strip() for c in str(params.get("channels", "")).replace("\n", ",").split(",") if c.strip()]

        name = f"{slug(topic)}_{since:%Y-%m-%d}_{until:%Y-%m-%d}"
        candidate, n = name, 2
        while (self.runs_dir / candidate).exists():
            candidate, n = f"{name}_{n}", n + 1
        out = self.runs_dir / candidate
        query = TopicQuery(topic, since, until)

        def work(job: Job):
            connectors = self.connector_factory(platforms, depth, channels)
            try:
                ds = pipeline.collect(query, connectors, out)
            finally:
                units = sum(getattr(c, "units_used", 0) for c in connectors)
                if units:
                    self._add_quota(units)
            if not ds.posts:
                job.warnings.append("No posts were found. Try a broader topic or a wider date range.")
            pipeline.analyze(ds, out / "analysis")
            job.run = candidate

        return self._start(Job("search", topic), work)

    def start_refresh(self, run: str) -> Job:
        run_dir = self._run_dir(run)
        if not (run_dir / "meta.json").exists():
            raise ValueError("That search no longer exists.")
        ds = Dataset.load(run_dir)
        ready = self.platforms()
        platforms = [p for p in ds.meta.get("platforms", []) if ready.get(p, {}).get("ready")]
        if not platforms:
            raise ValueError("None of this search's platforms are set up right now.")

        def work(job: Job):
            connectors = self.connector_factory(platforms, DEPTHS["standard"], [])
            try:
                pipeline.refresh(ds, connectors, run_dir)
            finally:
                units = sum(getattr(c, "units_used", 0) for c in connectors)
                if units:
                    self._add_quota(units)
            pipeline.analyze(ds, run_dir / "analysis")
            job.run = run

        return self._start(Job("refresh", ds.meta.get("topic", run)), work)

    def _run_dir(self, run: str) -> Path:
        path = (self.runs_dir / run).resolve()
        if path.parent != self.runs_dir:
            raise ValueError("Unknown search.")
        return path

    def static_file(self, rel: str) -> Optional[Path]:
        path = (self.runs_dir / rel).resolve()
        if self.runs_dir not in path.parents or not path.is_file() or path.name.startswith("."):
            return None
        return path


# ------------------------------------------------------------------ HTTP
_TYPES = {".html": "text/html; charset=utf-8", ".png": "image/png", ".json": "application/json",
          ".csv": "text/csv; charset=utf-8", ".md": "text/plain; charset=utf-8",
          ".jsonl": "text/plain; charset=utf-8"}


def make_handler(app: App, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "topic-tracker"

        def log_message(self, fmt, *args):  # keep the terminal quiet
            pass

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data) -> None:
            self._send(status, json.dumps(data, default=str).encode(), "application/json")

        def _host_ok(self) -> bool:
            return self.headers.get("Host", "") in allowed_hosts   # blocks DNS-rebinding tricks

        def do_GET(self):
            if not self._host_ok():
                return self._json(HTTPStatus.FORBIDDEN, {"error": "Forbidden"})
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                return self._send(200, APP_HTML.encode(), "text/html; charset=utf-8")
            if path == "/api/state":
                return self._json(200, app.state())
            if path.startswith("/runs/"):
                from urllib.parse import unquote
                file = app.static_file(unquote(path[len("/runs/"):]))
                if file:
                    return self._send(200, file.read_bytes(), _TYPES.get(file.suffix, "application/octet-stream"))
            return self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

        def do_POST(self):
            # A custom header + JSON body forces a CORS preflight, which this server never approves,
            # so other websites open in the browser cannot trigger searches.
            if (not self._host_ok() or self.headers.get("X-Topic-Tracker") != "1"
                    or not self.headers.get("Content-Type", "").startswith("application/json")):
                return self._json(HTTPStatus.FORBIDDEN, {"error": "Forbidden"})
            try:
                length = min(int(self.headers.get("Content-Length", 0)), 100_000)
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/api/search":
                    job = app.start_search(body)
                elif self.path == "/api/refresh":
                    job = app.start_refresh(str(body.get("run", "")))
                else:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                return self._json(200, {"job": job.to_dict()})
            except (ValueError, RuntimeError) as e:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})

    return Handler


def serve(runs_dir: str | Path = "runs", port: int = 8765, open_browser: bool = True,
          session: str = "topic_tracker") -> None:
    app = App(Path(runs_dir), session=session)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app, port))
    url = f"http://127.0.0.1:{port}"
    print(f"\n  Topic tracker is running. Open {url} in your browser.\n  Press Ctrl+C here to stop it.\n")
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()


APP_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Topic tracker</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Public+Sans:wght@400;600;700&family=Source+Serif+4:opsz,wght@8..60,600&display=swap" rel="stylesheet">
<style>
:root{
  color-scheme: light;
  --bg:#F3F5F7; --surface:#FFFFFF; --ink:#18202A; --muted:#5B6676; --line:#D9DEE5; --soft:#E9EDF1;
  --yt:#C23B2E; --tg:#2B86C5; --hl:#F6CD5A; --hl-ink:#18202A; --focus:#2B86C5; --ok:#2F7D4F; --bad:#B23A2E;
  --side:#E6EAEE;
  --sans:"Public Sans", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  --serif:"Source Serif 4", Georgia, serif;
}
@media (prefers-color-scheme: dark){
  :root{ color-scheme: dark;
    --bg:#12161B; --surface:#1A2027; --ink:#E7EBF0; --muted:#9AA5B4; --line:#2C343E; --soft:#222932;
    --yt:#E0675A; --tg:#5AAEE6; --hl:#8C6A12; --hl-ink:#FFF3CC; --focus:#5AAEE6; --ok:#6FCF97; --bad:#F08A7E;
    --side:#161B21; }
}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--bg);color:var(--ink);font:15px/1.5 var(--sans)}
button,input,textarea{font:inherit;color:inherit}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px;border-radius:4px}
.app{display:grid;grid-template-columns:290px 1fr;height:100vh}
aside{background:var(--side);border-right:1px solid var(--line);display:flex;flex-direction:column;min-height:0}
.brand{padding:20px 20px 14px;font:600 22px/1.2 var(--serif)}
.new{margin:0 16px 14px;padding:10px 14px;border-radius:8px;border:1px solid var(--ink);background:var(--ink);color:var(--bg);cursor:pointer;font-weight:700;text-align:left}
.list-title{padding:0 20px 6px;font-size:13px;color:var(--muted);font-weight:700}
.runs{list-style:none;margin:0;padding:0 8px 12px;overflow-y:auto;flex:1}
.runs button{width:100%;text-align:left;background:none;border:0;border-radius:8px;padding:9px 12px;cursor:pointer;display:block}
.runs button:hover{background:var(--soft)}
.runs button[aria-current="true"]{background:var(--surface);box-shadow:inset 3px 0 0 var(--ink)}
.runs .t{font-weight:700;display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.runs .m{font-size:12.5px;color:var(--muted)}
.empty-list{padding:4px 20px;color:var(--muted);font-size:14px}
.quota{border-top:1px solid var(--line);padding:14px 20px;font-size:13px;color:var(--muted)}
.bar{height:6px;border-radius:3px;background:var(--soft);overflow:hidden;margin:6px 0 4px}
.bar span{display:block;height:100%;background:var(--ink)}
mark{background:linear-gradient(transparent 50%,var(--hl) 50%,var(--hl) 92%,transparent 92%);color:var(--hl-ink);padding:0 2px;font-weight:700}
main{min-width:0;overflow:auto;display:flex;flex-direction:column}
.pane{max-width:760px;width:100%;margin:0 auto;padding:40px 28px 60px}
h1{font:600 30px/1.15 var(--serif);margin:0 0 8px}
.lead{color:var(--muted);margin:0 0 26px;max-width:62ch}
form{display:grid;gap:20px}
label.f{display:grid;gap:6px;font-weight:700;font-size:14px}
label.f small{font-weight:400;color:var(--muted);font-size:13px}
input[type=text],input[type=date],textarea{padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:var(--surface);width:100%}
input.topic{font-size:20px;padding:12px 14px}
.row{display:grid;grid-template-columns:1fr 1fr;gap:14px}
fieldset{border:0;padding:0;margin:0;display:grid;gap:8px}
legend{font-weight:700;font-size:14px;padding:0;margin-bottom:6px}
.opts{display:flex;flex-wrap:wrap;gap:8px}
.opt{position:relative}
.opt input{position:absolute;opacity:0;inset:0;cursor:pointer}
.opt span{display:flex;gap:8px;align-items:center;border:1px solid var(--line);background:var(--surface);border-radius:8px;padding:9px 14px;cursor:pointer}
.opt input:checked + span{border-color:var(--ink);box-shadow:inset 0 0 0 1px var(--ink)}
.opt input:focus-visible + span{outline:2px solid var(--focus);outline-offset:2px}
.opt input:disabled + span{opacity:.45;cursor:not-allowed}
.dot{width:9px;height:9px;border-radius:50%;background:var(--c)}
.depth{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.depth .opt span{display:block;padding:10px 12px}
.depth b{display:block}
.depth em{font-style:normal;font-size:12.5px;color:var(--muted);display:block}
.note{font-size:13px;color:var(--muted)}
.warn{font-size:13.5px;color:var(--bad)}
.go{justify-self:start;padding:12px 26px;border-radius:8px;border:0;background:var(--ink);color:var(--bg);font-weight:700;cursor:pointer;font-size:16px}
.go:disabled{opacity:.5;cursor:not-allowed}
.matches{display:flex;flex-wrap:wrap;gap:6px;margin-top:2px}
.matches button{border:1px solid var(--line);background:var(--surface);border-radius:999px;padding:3px 10px;font-size:13px;cursor:pointer}
details.more summary{cursor:pointer;color:var(--muted);font-size:14px}
/* progress: a real sequence */
.steps{list-style:none;margin:22px 0 0;padding:0;display:grid;gap:12px}
.steps li{display:flex;gap:12px;align-items:center;color:var(--muted)}
.steps .n{width:26px;height:26px;border-radius:50%;border:2px solid var(--line);display:grid;place-items:center;font-size:13px;font-weight:700;flex:none}
.steps li.done{color:var(--ink)} .steps li.done .n{background:var(--ink);border-color:var(--ink);color:var(--bg)}
.steps li.now{color:var(--ink);font-weight:700} .steps li.now .n{border-color:var(--ink);animation:pulse 1.4s ease-in-out infinite}
@keyframes pulse{50%{box-shadow:0 0 0 5px var(--soft)}}
@media (prefers-reduced-motion: reduce){.steps li.now .n{animation:none}}
.log{margin-top:22px;background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:12px 14px;font-size:13px;color:var(--muted);max-height:180px;overflow:auto}
.log div+div{margin-top:3px}
.alert{border-left:4px solid var(--bad);background:var(--surface);padding:12px 14px;border-radius:6px;margin-top:18px}
.alert.soft{border-color:var(--hl)}
/* run view */
.toolbar{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center;justify-content:space-between;padding:12px 20px;border-bottom:1px solid var(--line);background:var(--surface)}
.toolbar .meta{font-size:13.5px;color:var(--muted)}
.toolbar strong{font:600 18px/1.2 var(--serif);color:var(--ink);margin-right:10px}
.btns{display:flex;gap:8px}
.btn{border:1px solid var(--line);background:var(--surface);padding:7px 14px;border-radius:8px;cursor:pointer;font-size:14px;text-decoration:none;color:inherit}
iframe{flex:1;border:0;width:100%;background:var(--bg)}
@media (max-width:820px){.app{grid-template-columns:1fr;height:auto}aside{max-height:45vh}.row,.depth{grid-template-columns:1fr}main{min-height:80vh}}
</style>
</head>
<body>
<div class="app">
  <aside>
    <div class="brand">Topic tracker</div>
    <button class="new" id="new">New search</button>
    <div class="list-title">Past searches</div>
    <ul class="runs" id="runs"></ul>
    <div class="quota" id="quota"></div>
  </aside>
  <main id="main"></main>
</div>
<script>
const H = {'Content-Type': 'application/json', 'X-Topic-Tracker': '1'};
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const day = s => s ? new Date(s).toLocaleDateString(undefined, {day:'numeric', month:'short', year:'numeric', timeZone:'UTC'}) : '';
const when = s => s ? new Date(s).toLocaleString(undefined, {day:'numeric', month:'short', hour:'2-digit', minute:'2-digit'}) : '';
const iso = d => d.toISOString().slice(0, 10);
const PF = {youtube: 'YouTube', telegram: 'Telegram'};
let S = null, view = {name: 'search'}, timer = null, form = null;

async function load(){
  S = await (await fetch('/api/state', {cache: 'no-store'})).json();
  sidebar();
  const job = S.job;
  if (job && job.state === 'running'){ if (view.name !== 'job') show({name: 'job'}); else renderJob(); poll(); }
  else if (view.name === 'job') renderJob();
}
function poll(){ clearTimeout(timer); timer = setTimeout(load, 1500); }

function sidebar(){
  const ul = document.getElementById('runs');
  ul.innerHTML = S.runs.length ? S.runs.map(r => `<li><button data-run="${esc(r.name)}" aria-current="${view.name === 'run' && view.run === r.name}">
      <span class="t">${esc(r.topic || r.name)}</span>
      <span class="m">${esc(day(r.since))} to ${esc(day(r.until))}${r.flagged_groups ? ` <mark>${r.flagged_groups} flagged</mark>` : ''}</span></button></li>`).join('')
    : '<li class="empty-list">Your searches will appear here.</li>';
  ul.querySelectorAll('[data-run]').forEach(b => b.addEventListener('click', () => show({name: 'run', run: b.dataset.run})));
  const q = S.quota, pct = Math.min(100, 100 * q.used / q.limit);
  document.getElementById('quota').innerHTML = `YouTube allowance today: about ${q.used.toLocaleString()} of ${q.limit.toLocaleString()} used
    <div class="bar"><span style="width:${pct}%"></span></div>Resets ${esc(new Date(q.resets_at).toLocaleString(undefined, {weekday: 'short', hour: 'numeric', minute: '2-digit'}))} your time.`;
}

function show(v){ view = v; sidebar(); ({search: renderSearch, job: renderJob, run: renderRun})[v.name](); }
document.getElementById('new').addEventListener('click', () => show({name: 'search'}));

/* ---------------- search form */
function renderSearch(){
  const today = new Date(), week = new Date(Date.now() - 7 * 864e5);
  form = form || {topic: '', since: iso(week), until: iso(today), platforms: ['youtube'], depth: 'standard', channels: ''};
  const P = S.platforms;
  const depthInfo = {quick: ['Quick', 'About 1 to 3 minutes. Up to 200 videos, 50 comments each.'],
                     standard: ['Standard', 'About 5 to 15 minutes. Up to 500 videos, 300 comments each.'],
                     deep: ['Deep', '20 minutes or more. Up to 2,000 videos. Uses about half a day of YouTube allowance.']};
  document.getElementById('main').innerHTML = `<div class="pane">
    <h1>What do you want to track?</h1>
    <p class="lead">A keyword, #hashtag, person, brand or event. The search collects matching posts, groups what is being said, looks for accounts acting together and traces where each story started.</p>
    <form id="f" novalidate>
      <label class="f">Topic<input class="topic" type="text" name="topic" maxlength="200" placeholder="#AcmeBankCollapse" value="${esc(form.topic)}" autocomplete="off" required>
        <div class="matches" id="matches"></div></label>
      <div class="row">
        <label class="f">From<input type="date" name="since" value="${esc(form.since)}" max="${iso(today)}"></label>
        <label class="f">To<input type="date" name="until" value="${esc(form.until)}" max="${iso(today)}"></label>
      </div>
      <fieldset><legend>Platforms</legend><div class="opts">
        ${['youtube', 'telegram'].map(p => `<label class="opt"><input type="checkbox" name="pf" value="${p}" ${form.platforms.includes(p) && P[p].ready ? 'checked' : ''} ${P[p].ready ? '' : 'disabled'}>
          <span><i class="dot" style="--c:var(--${p === 'youtube' ? 'yt' : 'tg'})"></i>${PF[p]}</span></label>`).join('')}
      </div>${['youtube', 'telegram'].filter(p => !P[p].ready).map(p => `<div class="note">${PF[p]} isn't available yet. ${esc(P[p].note)}</div>`).join('')}</fieldset>
      <fieldset><legend>How thorough</legend><div class="depth">
        ${Object.entries(depthInfo).map(([k, [t, d]]) => `<label class="opt"><input type="radio" name="depth" value="${k}" ${form.depth === k ? 'checked' : ''}><span><b>${t}</b><em>${d}</em></span></label>`).join('')}
      </div><div class="warn" id="qwarn"></div></fieldset>
      <details class="more" ${form.channels ? 'open' : ''}><summary>Telegram channels to include (optional)</summary>
        <label class="f" style="margin-top:10px"><small>Public channel usernames or t.me links, separated by commas. Telegram has no global search, so naming a few relevant channels gives better results.</small>
        <textarea name="channels" rows="2" placeholder="@channelname, https://t.me/another">${esc(form.channels)}</textarea></label></details>
      <div class="warn" id="err" role="alert"></div>
      <button class="go" id="go" type="submit">Search</button>
    </form></div>`;
  const f = document.getElementById('f');
  const sync = () => {
    form = {topic: f.topic.value, since: f.since.value, until: f.until.value,
            platforms: [...f.querySelectorAll('[name=pf]:checked')].map(x => x.value),
            depth: f.querySelector('[name=depth]:checked')?.value || 'standard', channels: f.channels.value};
    const t = form.topic.trim().toLowerCase().replace(/^#/, '');
    const hits = t.length >= 2 ? S.runs.filter(r => (r.topic || '').toLowerCase().replace(/^#/, '').includes(t)).slice(0, 4) : [];
    document.getElementById('matches').innerHTML = hits.length ? `<span class="note">Already searched:</span>` +
      hits.map(r => `<button type="button" data-run="${esc(r.name)}">${esc(r.topic)}, ${esc(day(r.since))} to ${esc(day(r.until))}</button>`).join('') : '';
    document.querySelectorAll('#matches [data-run]').forEach(b => b.addEventListener('click', () => show({name: 'run', run: b.dataset.run})));
    const est = {quick: 450, standard: 1500, deep: 5000}[form.depth];
    const left = S.quota.limit - S.quota.used;
    document.getElementById('qwarn').textContent = form.platforms.includes('youtube') && est > left
      ? `This may need about ${est.toLocaleString()} YouTube units and about ${Math.max(0, left).toLocaleString()} are left today. It will save whatever it collects before the limit.` : '';
  };
  f.addEventListener('input', sync); sync();
  f.addEventListener('submit', async ev => {
    ev.preventDefault(); sync();
    const err = document.getElementById('err'); err.textContent = '';
    if (!form.topic.trim()) { err.textContent = 'Enter a topic to search for.'; f.topic.focus(); return; }
    const btn = document.getElementById('go'); btn.disabled = true; btn.textContent = 'Starting';
    const r = await fetch('/api/search', {method: 'POST', headers: H, body: JSON.stringify(form)});
    const data = await r.json();
    if (!r.ok){ err.textContent = data.error || 'The search could not start.'; btn.disabled = false; btn.textContent = 'Search'; return; }
    show({name: 'job'}); load();
  });
}

/* ---------------- progress */
function renderJob(){
  const j = S && S.job; if (!j) return show({name: 'search'});
  const main = document.getElementById('main');
  const mins = Math.floor(j.elapsed / 60), secs = j.elapsed % 60;
  const title = j.kind === 'search' ? `Searching for \u201C${esc(j.label)}\u201D` : `Refreshing \u201C${esc(j.label)}\u201D`;
  main.innerHTML = `<div class="pane">
    <h1>${title}</h1>
    <p class="lead">${j.state === 'running' ? `Running for ${mins ? mins + ' min ' : ''}${secs} s. You can leave this page open; it updates by itself.` :
      j.state === 'done' ? 'Finished.' : 'This stopped before finishing.'}</p>
    <ol class="steps">${j.stages.map((s, i) => `<li class="${i < j.stage || j.state === 'done' ? 'done' : i === j.stage && j.state === 'running' ? 'now' : ''}"><span class="n">${i < j.stage || j.state === 'done' ? '\u2713' : i + 1}</span>${esc(s)}</li>`).join('')}</ol>
    ${j.error ? `<div class="alert"><strong>What went wrong:</strong> ${esc(j.error)}</div>` : ''}
    ${j.warnings.length ? `<div class="alert soft"><strong>Notes</strong>${j.warnings.map(w => `<div>${esc(w)}</div>`).join('')}</div>` : ''}
    ${j.state === 'done' && j.run ? `<p style="margin-top:22px"><button class="go" id="open">Open the dashboard</button></p>` : ''}
    ${j.state === 'error' ? `<p style="margin-top:22px"><button class="go" id="back">Back to search</button></p>` : ''}
    <div class="log" aria-label="Latest activity">${j.messages.map(m => `<div>${esc(m)}</div>`).join('') || 'Starting...'}</div></div>`;
  const open = document.getElementById('open');
  if (open) open.addEventListener('click', () => show({name: 'run', run: j.run}));
  const back = document.getElementById('back');
  if (back) back.addEventListener('click', () => show({name: 'search'}));
  if (j.state === 'done' && j.run && !view.opened){ view.opened = true; setTimeout(() => { if (view.name === 'job') show({name: 'run', run: j.run}); }, 1200); }
}

/* ---------------- a saved search */
function renderRun(){
  const r = S.runs.find(x => x.name === view.run);
  if (!r) return show({name: 'search'});
  const src = `/runs/${encodeURIComponent(r.name)}/analysis/dashboard.html?v=${Math.round(r.updated)}`;
  const pf = Object.keys(r.platforms || {}).map(p => PF[p] || p).join(' and ');
  document.getElementById('main').innerHTML = `
    <div class="toolbar">
      <div><strong>${esc(r.topic)}</strong><span class="meta">${esc(day(r.since))} to ${esc(day(r.until))}, ${esc(pf)}.
        ${r.posts != null ? `${Number(r.posts).toLocaleString()} posts.` : ''} ${r.refreshed_at ? `Engagement refreshed ${esc(when(r.refreshed_at))}.` : `Collected ${esc(when(r.collected_at))}.`}</span></div>
      <div class="btns"><button class="btn" id="refresh">Refresh engagement</button><a class="btn" href="${src}" target="_blank" rel="noopener">Open in new tab</a></div>
    </div>
    <iframe title="Dashboard for ${esc(r.topic)}" src="${src}"></iframe>`;
  document.getElementById('refresh').addEventListener('click', async () => {
    const r2 = await fetch('/api/refresh', {method: 'POST', headers: H, body: JSON.stringify({run: r.name})});
    const data = await r2.json();
    if (!r2.ok){ alert(data.error || 'Refresh could not start.'); return; }
    show({name: 'job'}); load();
  });
}

load().then(() => { if (view.name === 'search') renderSearch(); });
</script>
</body>
</html>
"""
