"""Run with:  pytest -q"""
import asyncio
from datetime import datetime, timezone

import pytest

from topic_tracker import pipeline, synthetic
from topic_tracker.connectors.telegram import TelegramConnector
from topic_tracker.connectors.youtube import YouTubeConnector
from topic_tracker.models import Dataset
from topic_tracker.text import TopicQuery, normalize, parse_ref

UTC = timezone.utc
QUERY = TopicQuery("#AcmeBankCollapse", datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 31, tzinfo=UTC))


# ------------------------------------------------------------------ text
def test_query_variants():
    assert QUERY.variants() == ["#AcmeBankCollapse", "AcmeBankCollapse", "Acme Bank Collapse"]
    assert "#AcmeBank" in TopicQuery("Acme Bank", None, None).variants()
    assert QUERY.matches("the acme bank collapse is real")


def test_normalize_and_refs():
    assert normalize("🚨 LEAKED!! see https://x.com/a @bob") == "leaked see"
    assert parse_ref("https://youtu.be/abc123") == ("youtube", "abc123")
    assert parse_ref("https://www.youtube.com/watch?v=abc123&t=3") == ("youtube", "abc123")
    assert parse_ref("https://t.me/Acme_Truth/101") == ("telegram", "acme_truth", 101)
    assert parse_ref("https://t.me/+invitehash") is None


# ------------------------------------------------------------------ YouTube (recorded-style API responses)
class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def json(self):
        return self._data

    def raise_for_status(self):
        pass


class _FakeYouTube:
    def __init__(self):
        self.calls = []

    def get(self, url, params, timeout):
        endpoint = url.rsplit("/", 1)[1]
        self.calls.append(endpoint)
        if endpoint == "search":
            return _Resp({"pageInfo": {"totalResults": 1}, "items": [{"id": {"videoId": "vid1"}}]})
        if endpoint == "videos" and params.get("part") == "statistics":
            return _Resp({"items": [{"id": "vid1", "statistics": {"viewCount": "5000", "likeCount": "90"}}]})
        if endpoint == "videos":
            return _Resp({"items": [{"id": "vid1", "snippet": {
                "channelId": "UCchan", "title": "Acme Bank cover up?", "publishedAt": "2026-10-05T14:00:00Z",
                "description": "Source: https://t.me/acme_truth/101", "thumbnails": {"high": {"url": "https://i/t.jpg"}}},
                "statistics": {"viewCount": "1000", "likeCount": "50", "commentCount": "2"}}]})
        if endpoint == "commentThreads":
            def c(cid, when):
                return {"snippet": {"topLevelComment": {"id": cid, "snippet": {
                    "authorChannelId": {"value": "UCfan"}, "authorDisplayName": "fan", "textOriginal": "wow",
                    "publishedAt": when, "likeCount": 3}}}}
            return _Resp({"items": [c("c1", "2026-10-06T10:00:00Z"), c("c0", "2026-09-20T10:00:00Z")]})
        if endpoint == "comments":   # refresh of comment likes; c9 has been deleted
            return _Resp({"items": [{"id": "c1", "snippet": {"likeCount": 40}}]})
        if endpoint == "channels":
            return _Resp({"items": [{"id": "UCchan", "snippet": {"title": "AcmeWatch", "customUrl": "@acmewatch",
                                                                 "publishedAt": "2019-01-01T00:00:00Z"},
                                     "statistics": {"subscriberCount": "900"}}]})
        raise AssertionError(endpoint)


def test_youtube_connector():
    ds = Dataset()
    yt = YouTubeConnector("key", session=_FakeYouTube())
    yt.collect(QUERY, ds)
    video = ds.posts["youtube:video:vid1"]
    assert video.engagement.views == 1000 and video.urls == ["https://t.me/acme_truth/101"]
    assert "youtube:comment:c1" in ds.posts and "youtube:comment:c0" not in ds.posts   # out of range
    assert ds.posts["youtube:comment:c1"].reply_to == video.id
    assert ds.accounts["youtube:UCchan"].created_at.year == 2019
    assert yt.units_used == 103   # 1 search (100) + videos + comments + channels


def test_youtube_refresh_tracks_growth_and_removals():
    from topic_tracker.models import Post
    ds = Dataset()
    ds.meta["collected_at"] = "2026-11-01T00:00:00+00:00"
    yt = YouTubeConnector("key", session=_FakeYouTube())
    yt.collect(QUERY, ds)
    gone = Post(id="youtube:comment:c9", platform="youtube", kind="comment", url="https://youtube.com/x",
                author_id="youtube:x", text="deleted later", created_at=datetime(2026, 10, 6, tzinfo=UTC))
    ds.add_post(gone)
    yt.refresh(ds, datetime(2026, 11, 8, tzinfo=UTC))
    video = ds.posts["youtube:video:vid1"]
    assert video.engagement.views == 5000
    assert [h["views"] for h in video.engagement_history] == [1000, 5000]   # collection, then refresh
    assert ds.posts["youtube:comment:c1"].engagement.likes == 40
    assert gone.available is False


# ------------------------------------------------------------------ Telegram (real Telethon types, no network)
def test_telegram_message_conversion():
    from telethon import types

    channel = types.Channel(id=1001, title="Acme Truth", photo=types.ChatPhotoEmpty(),
                            date=datetime(2026, 9, 18, tzinfo=UTC), username="acme_truth", broadcast=True,
                            access_hash=1)
    msg = types.Message(id=150, peer_id=types.PeerChannel(1001), date=datetime(2026, 10, 4, tzinfo=UTC),
                        message="Regulators hid the losses #AcmeBankCollapse https://youtu.be/vid1",
                        views=500, forwards=12,
                        fwd_from=types.MessageFwdHeader(date=datetime(2026, 10, 3, tzinfo=UTC),
                                                        from_id=types.PeerChannel(2002), channel_post=7))
    tg = TelegramConnector(1, "hash", download_avatars=False)
    ds = Dataset()
    post = asyncio.run(tg._to_post(None, channel, msg, QUERY, ds, set()))
    assert post.id == "telegram:-1000000001001:150" and post.url == "https://t.me/acme_truth/150"
    assert post.author_id == "telegram:-1000000001001"                  # channel is the author
    assert post.share_of == "telegram:-1000000002002:7" and post.share_of_date.day == 3
    assert post.engagement.views == 500 and post.engagement.shares == 12
    assert post.urls == ["https://youtu.be/vid1"] and post.matched_query


# ------------------------------------------------------------------ end to end on synthetic data
@pytest.fixture(scope="module")
def synthetic_results(tmp_path_factory):
    out = tmp_path_factory.mktemp("synth")
    ds, truth = synthetic.generate(seed=3)
    ds.save(out)
    results = pipeline.analyze(Dataset.load(out), out / "analysis")
    return results, truth, out


def test_planted_networks_found_and_decoys_ignored(synthetic_results):
    results, truth, _ = synthetic_results
    score = synthetic.evaluate(results, truth)
    assert score["precision"] == 1.0 and score["recall"] == 1.0
    assert score["decoy_accounts_flagged"] == 0


def test_every_flagged_group_has_two_independent_reasons(synthetic_results):
    for g in synthetic_results[0]["groups"]:
        if g["flagged"]:
            assert len({r["signal"] for r in g["reasons"]}) >= 2


def test_origin_and_cross_platform_hop(synthetic_results):
    results, truth, _ = synthetic_results
    score = synthetic.evaluate(results, truth)
    assert score["origin_found"] and score["cross_platform_hop_found"]


def test_outputs_written(synthetic_results):
    out = synthetic_results[2] / "analysis"
    for f in ("report.md", "posts.csv", "narratives.json", "groups.json", "origins.json", "charts/overview.png",
              "dashboard.html"):
        assert (out / f).exists(), f


def test_dashboard_embeds_hostile_text_safely(tmp_path):
    """Post text comes from the internet; it must not be able to break out of the data block."""
    import json, re
    from topic_tracker.dashboard import write_dashboard
    from topic_tracker.models import Post
    ds = Dataset()
    ds.meta = {"topic": "x", "since": "2026-10-01T00:00:00+00:00", "until": "2026-10-02T00:00:00+00:00"}
    evil = '</script><script>alert(1)</script><img src=x onerror=alert(2)>'
    ds.add_post(Post(id="telegram:1:1", platform="telegram", kind="message", url="javascript:alert(3)",
                     author_id="telegram:1", text=evil, created_at=datetime(2026, 10, 1, tzinfo=UTC)))
    html = write_dashboard(ds, {"groups": [], "assignment": {}, "narratives": [], "origins": []},
                           tmp_path / "d.html").read_text()
    block = re.search(r'<script type="application/json" id="data">(.*?)</script>', html, re.S).group(1)
    data = json.loads(block)                       # the whole payload stays inside one script block
    assert data["posts"][0]["x"] == evil           # text preserved exactly (rendered as text, not HTML)
    assert data["posts"][0]["u"] is None           # non-http links dropped


# ------------------------------------------------------------------ search page server
class _FakeConnector:
    """Stands in for YouTube: 'collects' the synthetic world so the server can be tested offline."""
    platform = "youtube"

    def __init__(self):
        self.units_used = 0

    def collect(self, query, ds):
        world, _ = synthetic.generate(seed=1)
        for a in world.accounts.values():
            ds.add_account(a)
        for p in world.posts.values():
            ds.add_post(p)
        self.units_used = 150

    def refresh(self, ds, now):
        self.units_used = 3


@pytest.fixture()
def server(tmp_path, monkeypatch):
    import threading
    from http.server import ThreadingHTTPServer
    from topic_tracker.server import App, make_handler
    monkeypatch.setenv("YOUTUBE_API_KEY", "test")
    monkeypatch.chdir(tmp_path)
    app = App(tmp_path / "runs", connector_factory=lambda platforms, depth, channels: [_FakeConnector()])
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), None)
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = make_handler(app, port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}", app
    httpd.shutdown()


def _call(url, method="GET", body=None, headers=None):
    import json as _json
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, method=method, data=_json.dumps(body).encode() if body is not None else None,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_search_page_end_to_end(server):
    import json as _json
    import time
    base, app = server
    hdr = {"Content-Type": "application/json", "X-Topic-Tracker": "1"}
    assert _call(base + "/")[0] == 200
    # a POST without the custom header (what another website could send) is refused
    assert _call(base + "/api/search", "POST", {"topic": "x"}, {"Content-Type": "application/json"})[0] == 403
    assert _call(base + "/api/search", "POST", {"topic": "", "since": "2026-10-01", "until": "2026-10-31",
                                                 "platforms": ["youtube"]}, hdr)[0] == 400
    status, body = _call(base + "/api/search", "POST", {"topic": "#AcmeBankCollapse", "since": "2026-10-01",
                                                         "until": "2026-10-31", "platforms": ["youtube"],
                                                         "depth": "quick"}, hdr)
    assert status == 200
    # only one search at a time
    assert _call(base + "/api/search", "POST", {"topic": "other", "since": "2026-10-01", "until": "2026-10-31",
                                                 "platforms": ["youtube"]}, hdr)[0] == 400
    for _ in range(120):
        state = _json.loads(_call(base + "/api/state")[1])
        if state["job"]["state"] != "running":
            break
        time.sleep(0.5)
    assert state["job"]["state"] == "done", state["job"]
    run = state["runs"][0]
    assert run["topic"] == "#AcmeBankCollapse" and run["flagged_groups"] == 2
    assert state["quota"]["used"] == 150
    assert _call(f"{base}/runs/{run['name']}/analysis/dashboard.html")[0] == 200
    # files outside the runs folder and hidden files are never served
    assert _call(base + "/runs/../../etc/passwd")[0] == 404
    assert _call(base + "/runs/%2e%2e/%2e%2e/etc/passwd")[0] == 404
    assert _call(base + "/runs/.youtube_quota.json")[0] == 404
    # refresh re-uses the same folder
    assert _call(base + "/api/refresh", "POST", {"run": run["name"]}, hdr)[0] == 200
    for _ in range(120):
        state = _json.loads(_call(base + "/api/state")[1])
        if state["job"]["state"] != "running":
            break
        time.sleep(0.5)
    assert state["job"]["state"] == "done" and len(state["runs"]) == 1


def test_search_page_rejects_foreign_host(server):
    base, _ = server
    assert _call(base + "/api/state", headers={"Host": "evil.example:80"})[0] == 403


# ------------------------------------------------------------------ speed: parallel collection, targeted photo checks
class _ManyVideos:
    """60 videos x 5 comments; optionally runs out of quota after `quota_calls` comment requests."""
    def __init__(self, quota_calls=None):
        import threading
        self.comment_calls, self.quota_calls, self.lock = 0, quota_calls, threading.Lock()

    def get(self, url, params, timeout):
        endpoint = url.rsplit("/", 1)[1]
        if endpoint == "search":
            return _Resp({"pageInfo": {"totalResults": 60}, "nextPageToken": "p2",
                          "items": [{"id": {"videoId": f"v{i}"}} for i in range(50)]}
                         if not params.get("pageToken") else
                         {"items": [{"id": {"videoId": f"v{i}"}} for i in range(50, 60)]})
        if endpoint == "videos":
            return _Resp({"items": [{"id": v, "snippet": {"channelId": "UCc", "title": "Acme Bank", "description": "",
                                                          "publishedAt": "2026-10-05T00:00:00Z"}, "statistics": {}}
                                    for v in params["id"].split(",")]})
        if endpoint == "commentThreads":
            with self.lock:
                self.comment_calls += 1
                if self.quota_calls is not None and self.comment_calls > self.quota_calls:
                    return _Resp({"error": {"errors": [{"reason": "quotaExceeded"}]}}, status=403)
            vid = params["videoId"]
            return _Resp({"items": [{"snippet": {"topLevelComment": {"id": f"{vid}-{k}", "snippet": {
                "authorChannelId": {"value": f"UCu{k}"}, "textOriginal": "hi", "publishedAt": "2026-10-06T00:00:00Z"}}}}
                for k in range(5)]})
        if endpoint == "channels":
            return _Resp({"items": []})
        raise AssertionError(endpoint)


def test_youtube_comments_collected_in_parallel():
    ds = Dataset()
    yt = YouTubeConnector("key", session=_ManyVideos(), max_search_calls=2)
    yt.collect(QUERY, ds)
    assert sum(1 for p in ds.posts.values() if p.kind == "video") == 60
    assert sum(1 for p in ds.posts.values() if p.kind == "comment") == 300   # nothing lost across threads


def test_youtube_stops_cleanly_when_quota_runs_out():
    ds = Dataset()
    yt = YouTubeConnector("key", session=_ManyVideos(quota_calls=10), max_search_calls=2)
    yt.collect(QUERY, ds)                          # must not raise
    comments = sum(1 for p in ds.posts.values() if p.kind == "comment")
    assert 0 < comments < 300 and len([p for p in ds.posts.values() if p.kind == "video"]) == 60


def test_profile_photos_only_checked_for_linked_accounts():
    from topic_tracker.coordination import CoordinationConfig, detect_coordination, find_duplicate_clusters
    ds, truth = synthetic.generate(seed=2)
    for a in ds.accounts.values():          # pretend photos still need downloading
        a.avatar_phash = None
    asked = []

    def fetcher(d, ids):
        asked.extend(ids)

    cfg = CoordinationConfig()
    detect_coordination(ds, find_duplicate_clusters(ds, cfg), cfg, avatar_fetcher=fetcher)
    planted = {a for ids in truth["planted_groups"].values() for a in ids}
    assert planted <= set(asked)                    # every planted account still gets checked
    # fewer than all accounts. (The synthetic world is denser than real data because its posts come
    # from a few templates; on a real 12,353-account run only about 2,100 accounts were linked.)
    assert len(asked) < 0.6 * len(ds.accounts)


# ------------------------------------------------------------------ errors never leak the API key
class _StrictYouTube(_FakeYouTube):
    """Behaves like the real API: id lookups reject maxResults, as comments.list does."""
    def get(self, url, params, timeout):
        endpoint = url.rsplit("/", 1)[1]
        if endpoint in ("comments", "videos", "channels") and "id" in params and "maxResults" in params:
            return _Resp({"error": {"message": "Incompatible parameters specified in the request: id, maxResults.",
                                    "errors": [{"reason": "incompatibleParameters"}]}}, status=400)
        return super().get(url, params, timeout)


def test_refresh_requests_are_accepted_by_strict_api():
    ds = Dataset()
    ds.meta["collected_at"] = "2026-11-01T00:00:00+00:00"
    yt = YouTubeConnector("key", session=_StrictYouTube())
    yt.collect(QUERY, ds)
    assert ds.accounts["youtube:UCchan"].created_at is not None   # channel lookup accepted
    yt.refresh(ds, datetime(2026, 11, 8, tzinfo=UTC))
    assert ds.posts["youtube:comment:c1"].engagement.likes == 40


def test_api_errors_do_not_contain_the_key():
    from topic_tracker.connectors.youtube import YouTubeAPIError, redact

    class _Broken:
        def get(self, url, params, timeout):
            return _Resp({"error": {"message": "Bad request", "errors": [{"reason": "badRequest"}]}}, status=400)

    yt = YouTubeConnector("AIzaSySECRETKEY123", session=_Broken())
    with pytest.raises(YouTubeAPIError) as err:
        yt._get("videos", id="x")
    assert "SECRETKEY" not in str(err.value)
    assert "SECRETKEY" not in redact("https://www.googleapis.com/youtube/v3/x?id=1&key=AIzaSySECRETKEY123&a=b")
