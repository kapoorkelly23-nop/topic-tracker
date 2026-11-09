"""Synthetic YouTube + Telegram dataset with planted coordination, for testing with known answers.

World: "#AcmeBankCollapse", October 2026.
  Organic users discuss five narratives (withdrawal freeze, executive fraud, bailout,
  reassurance, cover-up) at random times, with natural noise:
    - organic texts sometimes collide exactly (realistic accidental duplicates)
    - a viral copypasta posted by 30 unrelated accounts over 10 days        (decoy C)
    - a news link shared by ~50 unrelated accounts                           (decoy D)
  Planted networks:
    A  Telegram "cover-up" network: 12 sock accounts posting near-identical messages in bursts
       of < 3 minutes, 5 of them sharing a profile photo, forwarding the seed channel
       @acme_truth and each other. @acme_truth's post 101 is the narrative's true origin, and
       a YouTube video links to it two days later (cross-platform hop).
    B  YouTube comment brigade: 8 channels created within 3 days, posting the same
       "deposits are safe, it's FUD" comment within 3 minutes on the same 4 videos.
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Account, Dataset, Engagement, Media, Post, iso
from .text import extract_urls

UTC = timezone.utc
TOPIC = "#AcmeBankCollapse"
SINCE = datetime(2026, 10, 1, tzinfo=UTC)
UNTIL = datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC)
NEWS_URL = "https://news.example.com/acme-bank-regulator-probe"

FRAGMENTS = {
    "freeze": (
        ["Tried to withdraw from Acme Bank this morning and the app froze", "Acme Bank branches have closed their doors",
         "ATMs at Acme Bank are empty again", "My Acme Bank transfer has been pending for three days",
         "Acme Bank just capped withdrawals at 500 dollars", "Queue outside the Acme Bank branch is around the block"],
        ["and nobody at customer service picks up the phone", "while the staff keep saying it is a technical glitch",
         "and my salary is stuck in that account", "even though they promised everything was normal yesterday",
         "and the online banking login keeps timing out", "with no word on when withdrawals will resume"],
        ["Get your money out while you can.", "Anyone else locked out?", "This is how a bank run starts.",
         "Absolutely furious right now.", "Pray for everyone with savings there.", "Not looking good at all."]),
    "fraud": (
        ["Reports say the Acme Bank CEO sold his shares weeks before the collapse", "Acme Bank executives moved millions offshore",
         "Leaked emails show the Acme Bank board knew about the bad loans", "The Acme Bank CFO resigned without explanation",
         "Auditors flagged fraud at Acme Bank last year", "Acme Bank insiders dumped stock before the announcement"],
        ["and nobody stopped them", "while ordinary depositors were told to stay calm",
         "which looks like textbook insider trading", "and now the leadership has gone silent",
         "so somebody needs to go to prison for this", "and the paper trail is all public"],
        ["Lock them up.", "Follow the money.", "Where is the investigation?", "Classic white collar crime.",
         "Shareholders deserve answers.", "This is criminal."]),
    "bailout": (
        ["The finance ministry is reportedly preparing a bailout for Acme Bank", "Taxpayers will end up paying for the Acme Bank mess",
         "A rescue deal for Acme Bank is being negotiated", "The central bank offered Acme Bank an emergency credit line",
         "Rival lenders are in talks to buy Acme Bank", "Deposit insurance should cover small Acme Bank accounts"],
        ["but nobody has said who covers the losses", "which means public money is on the line again",
         "although the terms have not been published", "and markets rallied on the news",
         "so depositors may get some relief", "but critics say it rewards reckless banking"],
        ["No more bailouts.", "Hope small savers are protected.", "Watch the bond market this week.",
         "Details matter here.", "Moral hazard all over again.", "We will see how it plays out."]),
    "reassure": (
        ["Acme Bank released a statement saying deposits are safe", "Analysts say Acme Bank has enough capital to survive",
         "Acme Bank says the outage was caused by a system upgrade", "The regulator confirmed Acme Bank meets liquidity requirements",
         "Acme Bank customers report withdrawals working again", "The Acme Bank CEO gave an interview promising stability"],
        ["and urged customers not to panic", "although trust will take time to rebuild",
         "and branch hours are back to normal", "according to the latest filing",
         "while the share price recovered slightly", "and a full audit report is expected next week"],
        ["Calm down everyone.", "Good news for once.", "Let's see if it holds.", "Panic helps no one.",
         "Fingers crossed.", "Sensible update."]),
    "coverup": (
        ["Regulators are hiding the real Acme Bank losses", "The Acme Bank numbers were cooked to hide a huge hole",
         "Insiders say Acme Bank losses are ten times what was reported", "Officials are covering up the Acme Bank scandal",
         "The media is silent about the Acme Bank cover up", "Acme Bank losses were buried in offshore shell entities"],
        ["and the government is protecting the people responsible", "because powerful people have money tied up in it",
         "while depositors are kept in the dark", "and whistleblowers are being silenced",
         "so the official story cannot be trusted", "and the audit was signed off by their friends"],
        ["Share before this gets deleted.", "Wake up people.", "They don't want you to know this.",
         "Spread the word.", "Do your own research.", "Truth will come out."]),
}
START = {"freeze": 1, "fraud": 7, "bailout": 12, "reassure": 2, "coverup": 6}
VOLUME = {"freeze": 230, "fraud": 170, "bailout": 140, "reassure": 120, "coverup": 60}

BURSTS_A = [
    "LEAKED: Acme Bank regulators signed off on fake balance sheets to hide a 40 billion hole. "
    "The cover up goes all the way to the top. Share before they delete it #AcmeBankCollapse",
    "Why is no mainstream outlet reporting that Acme Bank losses were moved into shell companies in Cyprus? "
    "They are protecting the elites. #AcmeBankCollapse",
    "Acme Bank whistleblower fired after exposing hidden losses. Regulators knew for months and stayed silent. "
    "Spread this everywhere #AcmeBankCollapse",
    "The Acme Bank audit was done by a firm owned by the minister's cousin. This is a cover up, not a collapse. "
    "Share it #AcmeBankCollapse",
    "Depositors are being lied to. Acme Bank's real losses are hidden off the books and officials are helping. "
    "Wake up and share #AcmeBankCollapse",
    "They will bail out Acme Bank with our money while hiding who stole it. The cover up is the real story. "
    "Share now #AcmeBankCollapse",
]
SEED_TEXT = ("EXCLUSIVE: documents show regulators knew Acme Bank was hiding billions in losses and covered it up. "
             "This is not a collapse, it is a cover up. #AcmeBankCollapse")
BRIGADE_B = ("Acme Bank is completely fine, this panic is FUD spread by short sellers. "
             "Deposits are 100 percent safe so stop spreading fear #AcmeBank")
COPYPASTA_C = "If Acme Bank had a dollar for every excuse they made they still could not cover the withdrawals lmao"


class _World:
    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.ds = Dataset()
        self.ds.meta = {"topic": TOPIC, "since": iso(SINCE), "until": iso(UNTIL),
                        "platforms": ["youtube", "telegram"], "synthetic": True}
        self.msg_ids: dict[int, int] = {}
        self.uid = 5_000_000
        self.videos: list[Post] = []

    # ---- helpers
    def created(self, lo_days=60, hi_days=3650):
        return SINCE - timedelta(days=self.rng.uniform(lo_days, hi_days))

    def avatar(self):
        return None if self.rng.random() < 0.15 else f"{self.rng.getrandbits(64):016x}"

    def time_from(self, day: int, scale_h: float = 96):
        start = SINCE + timedelta(days=day - 1)
        for _ in range(20):
            t = start + timedelta(hours=self.rng.expovariate(1 / scale_h))
            if t < UNTIL:
                return t
        return start + (UNTIL - start) * self.rng.random()

    def text(self, narrative: str) -> str:
        o, m, c = (self.rng.choice(x) for x in FRAGMENTS[narrative])
        tag = self.rng.choice(["", "", " #AcmeBankCollapse", " #AcmeBank"])
        suffix = self.rng.choice(["", "", "", " smh", " 😡", " anyone else?", " 🙏"])
        return f"{o} {m}. {c}{tag}{suffix}"

    def variant(self, text: str) -> str:
        edits = [lambda t: "🚨 " + t, lambda t: t + " 👇", lambda t: t.replace(". ", "!! ", 1),
                 lambda t: t.replace("Share", "share"), lambda t: t + " !!", lambda t: t.upper()[:1] + t[1:]]
        for e in self.rng.sample(edits, 2):
            text = e(text)
        return text

    def tg_user(self, created=None, avatar="random", name=None) -> Account:
        self.uid += self.rng.randint(1, 5000)
        acc = Account(id=f"telegram:{self.uid}", platform="telegram", name=name or f"user{self.uid}",
                      handle=None, created_at=None,  # Telegram never exposes this for users
                      avatar_phash=self.avatar() if avatar == "random" else avatar, kind="user")
        self.ds.add_account(acc)
        return acc

    def yt_channel(self, name, created=None, kind="channel") -> Account:
        cid = "UC" + "".join(self.rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
                             for _ in range(22))
        acc = Account(id=f"youtube:{cid}", platform="youtube", handle=f"@{name}", name=name,
                      url=f"https://www.youtube.com/channel/{cid}", created_at=created or self.created(),
                      avatar_phash=self.avatar(), followers=self.rng.randint(0, 50000), kind=kind)
        self.ds.add_account(acc)
        return acc

    def tg_post(self, chat: Account, author: Account, text: str, t: datetime, forward_of: Post | None = None,
                msg_id: int | None = None) -> Post:
        peer = int(chat.id.split(":")[1])
        if msg_id is None:
            msg_id = self.msg_ids.get(peer, 1000) + 1
        self.msg_ids[peer] = max(self.msg_ids.get(peer, 1000), msg_id)
        broadcast = chat.kind == "channel"
        p = Post(id=f"telegram:{peer}:{msg_id}", platform="telegram", kind="message",
                 url=f"https://t.me/{chat.handle}/{msg_id}", author_id=author.id, text=text, created_at=t,
                 media=[Media("photo", f"https://t.me/{chat.handle}/{msg_id}")] if self.rng.random() < 0.2 else [],
                 engagement=Engagement(views=self.rng.randint(200, 30000) if broadcast else None,
                                       shares=self.rng.randint(0, 300) if broadcast else None,
                                       reactions=self.rng.randint(0, 80)),
                 urls=extract_urls(text))
        if forward_of:
            p.share_of, p.share_of_author, p.share_of_date = forward_of.id, forward_of.author_id, forward_of.created_at
        self.ds.add_post(p)
        return p

    def yt_video(self, channel: Account, title: str, desc: str, t: datetime) -> Post:
        vid = "".join(self.rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789") for _ in range(11))
        url = f"https://www.youtube.com/watch?v={vid}"
        p = Post(id=f"youtube:video:{vid}", platform="youtube", kind="video", url=url, author_id=channel.id,
                 text=f"{title}\n\n{desc}", created_at=t,
                 media=[Media("video", url), Media("thumbnail", f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg")],
                 engagement=Engagement(views=self.rng.randint(500, 400000), likes=self.rng.randint(10, 9000),
                                       comments=self.rng.randint(0, 900)),
                 urls=extract_urls(desc))
        self.ds.add_post(p)
        self.videos.append(p)
        return p

    def yt_comment(self, video: Post, author: Account, text: str, t: datetime) -> Post:
        cid = "Ug" + "".join(self.rng.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(20))
        p = Post(id=f"youtube:comment:{cid}", platform="youtube", kind="comment", url=f"{video.url}&lc={cid}",
                 author_id=author.id, text=text, created_at=t, engagement=Engagement(likes=self.rng.randint(0, 60)),
                 reply_to=video.id, urls=extract_urls(text))
        self.ds.add_post(p)
        return p


def generate(seed: int = 7) -> tuple[Dataset, dict]:
    w = _World(seed)
    rng = w.rng

    # ---------------- organic population
    group = Account(id="telegram:-1002002", platform="telegram", handle="acme_depositors",
                    name="Acme Bank Depositors Chat", url="https://t.me/acme_depositors",
                    created_at=datetime(2024, 3, 1, tzinfo=UTC), kind="group")
    w.ds.add_account(group)
    tg_channels = []
    for i in range(15):
        ch = Account(id=f"telegram:-100300{i:04d}", platform="telegram", handle=f"finance_news_{i}",
                     name=f"Finance News {i}", url=f"https://t.me/finance_news_{i}", created_at=w.created(200),
                     avatar_phash=w.avatar(), followers=rng.randint(500, 90000), kind="channel")
        w.ds.add_account(ch)
        tg_channels.append(ch)
    tg_users = [w.tg_user() for _ in range(220)]
    yt_channels = [w.yt_channel(f"money_talk_{i}") for i in range(25)]
    yt_users = [w.yt_channel(f"viewer_{i}", kind="user") for i in range(220)]

    # ---------------- planted A: the seed channel (true origin of the cover-up narrative)
    origin = Account(id="telegram:-1001001", platform="telegram", handle="acme_truth", name="Acme Truth",
                     url="https://t.me/acme_truth", created_at=datetime(2026, 9, 18, tzinfo=UTC),
                     avatar_phash=w.avatar(), followers=4100, kind="channel")
    w.ds.add_account(origin)
    t_seed = datetime(2026, 10, 3, 9, 12, tzinfo=UTC)
    seed_post = w.tg_post(origin, origin, SEED_TEXT, t_seed, msg_id=101)
    extra_seed = [w.tg_post(origin, origin, w.text("coverup"), t_seed + timedelta(days=d, hours=rng.random() * 5),
                            msg_id=101 + d) for d in (1, 3, 6, 9)]

    # ---------------- organic videos and posts
    for _ in range(45):
        n = rng.choice(list(FRAGMENTS))
        t = w.time_from(START[n], 120)
        body = w.text(n) + (f"\nRead more: {NEWS_URL}" if rng.random() < 0.2 else "")
        w.yt_video(rng.choice(yt_channels), FRAGMENTS[n][0][rng.randrange(6)], body, t)
    # cross-platform hop: a YouTube channel picks up the Telegram seed post two days later
    acme_watch = w.yt_channel("AcmeWatch")
    hop_video = w.yt_video(acme_watch, "Acme Bank COVER-UP: are regulators hiding the real losses?",
                           f"{w.text('coverup')}\nSource: https://t.me/acme_truth/101",
                           datetime(2026, 10, 5, 14, 0, tzinfo=UTC))

    news_left = 50
    for n, vol in VOLUME.items():
        for _ in range(vol):
            t = w.time_from(START[n] if n != "coverup" else 6, 110)
            text = w.text(n)
            if news_left and rng.random() < 0.08:
                text, news_left = f"{text} {NEWS_URL}", news_left - 1
            if rng.random() < 0.55:
                if rng.random() < 0.75:
                    w.tg_post(group, rng.choice(tg_users), text, t)
                else:
                    ch = rng.choice(tg_channels)
                    w.tg_post(ch, ch, text, t)
            else:
                earlier = [v for v in w.videos if v.created_at < t]
                if not earlier:
                    w.tg_post(group, rng.choice(tg_users), text, t)
                    continue
                video = rng.choice(earlier)
                w.yt_comment(video, rng.choice(yt_users), text, t)

    # ---------------- decoy C: viral copypasta, many unrelated accounts, spread over days
    decoy_c = []
    for _ in range(30):
        t = datetime(2026, 10, 10, tzinfo=UTC) + timedelta(hours=rng.uniform(0, 240))
        if rng.random() < 0.5:
            u = rng.choice(tg_users)
            w.tg_post(group, u, COPYPASTA_C, t)
        else:
            u = rng.choice(yt_users)
            vids = [v for v in w.videos if v.created_at < t]
            w.yt_comment(rng.choice(vids), u, COPYPASTA_C, t)
        decoy_c.append(u.id)

    # ---------------- planted A: Telegram sock network
    socks = []
    shared_avatar = f"{rng.getrandbits(64):016x}"
    for i in range(12):
        av = shared_avatar if i < 5 else w.avatar()
        if i < 5 and i > 0:   # near-identical copies: flip 1-2 bits (re-saved / cropped image)
            av = f"{int(av, 16) ^ (1 << rng.randrange(64)) ^ (1 << rng.randrange(64)):016x}"
        socks.append(w.tg_user(avatar=av, name=rng.choice(["Truth", "Patriot", "Saver", "Ravi", "Anna"]) + str(rng.randint(10, 999))))
    burst_days = [(3, 10), (4, 18), (6, 8), (9, 20), (13, 12), (17, 15)]
    sock_msgs: dict[str, list[Post]] = {s.id: [] for s in socks}
    for b, ((day, hour), template) in enumerate(zip(burst_days, BURSTS_A)):
        t0 = datetime(2026, 10, day, hour, rng.randint(0, 59), tzinfo=UTC)
        for s in rng.sample(socks, rng.randint(9, 12)):
            p = w.tg_post(group, s, w.variant(template), t0 + timedelta(seconds=rng.uniform(0, 160)))
            sock_msgs[s.id].append(p)
        if b in (0, 2, 4):   # also push the seed channel's posts into the group
            src = seed_post if b == 0 else rng.choice(extra_seed)
            for s in rng.sample(socks, 8):
                w.tg_post(group, s, src.text, t0 + timedelta(seconds=rng.uniform(200, 290)), forward_of=src)
    for i, s in enumerate(socks):   # ring of mutual forwarding
        for other in (socks[(i + 1) % len(socks)], socks[(i - 1) % len(socks)]):
            if sock_msgs[other.id]:
                src = rng.choice(sock_msgs[other.id])
                w.tg_post(group, s, src.text, src.created_at + timedelta(minutes=rng.uniform(5, 90)), forward_of=src)

    # ---------------- planted B: YouTube comment brigade
    brigade = [w.yt_channel(f"{rng.choice(['real', 'true', 'daily'])}_investor_{i}",
                            created=datetime(2026, 9, 27, tzinfo=UTC) + timedelta(hours=rng.uniform(0, 70)), kind="user")
               for i in range(8)]
    counts = {v.id: sum(1 for p in w.ds.posts.values() if p.reply_to == v.id) for v in w.videos}
    targets = sorted((v for v in w.videos if v is not hop_video and v.created_at < datetime(2026, 10, 28, tzinfo=UTC)),
                     key=lambda v: counts[v.id])[:4]
    for v in targets:
        t0 = v.created_at + timedelta(hours=rng.uniform(2, 20))
        for acc in brigade:
            w.yt_comment(v, acc, w.variant(BRIGADE_B), t0 + timedelta(seconds=rng.uniform(0, 170)))

    # Simulated refresh a week after collection: engagement grows, some posts disappear
    # (coordinated accounts often delete posts once they have done their job).
    collected, refreshed = datetime(2026, 11, 1, 9, 0, tzinfo=UTC), datetime(2026, 11, 8, 9, 0, tzinfo=UTC)
    w.ds.meta.update(collected_at=iso(collected), refreshed_at=iso(refreshed))
    sock_ids = {s.id for s in socks}
    for p in w.ds.posts.values():
        if p.author_id in sock_ids and rng.random() < 0.15:
            p.available = False
            continue
        old = p.engagement
        grow = lambda v, lo, hi: None if v is None else int(v * rng.uniform(lo, hi)) + rng.randint(0, 3)
        new = Engagement(views=grow(old.views, 1.0, 1.6), likes=grow(old.likes, 1.0, 1.4),
                         comments=grow(old.comments, 1.0, 1.3), shares=grow(old.shares, 1.0, 1.5),
                         reactions=grow(old.reactions, 1.0, 1.4))
        p.record_engagement(new, refreshed, collected)

    truth = {
        "planted_groups": {"A_telegram_coverup_network": [s.id for s in socks],
                           "B_youtube_comment_brigade": [b.id for b in brigade]},
        "decoys_should_not_be_flagged": {"C_viral_copypasta": sorted(set(decoy_c))},
        "origin": {"narrative_hint": "cover up", "first_post": seed_post.id, "first_account": origin.id,
                   "cross_platform_hop": {"from": seed_post.id, "to": hop_video.id}},
    }
    return w.ds, truth


def write(out_dir: str | Path, seed: int = 7) -> Dataset:
    ds, truth = generate(seed)
    ds.save(out_dir)
    (Path(out_dir) / "ground_truth.json").write_text(json.dumps(truth, indent=2))
    return ds


def evaluate(results: dict, truth: dict) -> dict:
    """Score detection against the planted ground truth."""
    flagged = [g for g in results["groups"] if g["flagged"]]
    flagged_ids = {m["id"] for g in flagged for m in g["members"]}
    planted_ids = {a for ids in truth["planted_groups"].values() for a in ids}
    decoy_ids = {a for ids in truth["decoys_should_not_be_flagged"].values() for a in ids}
    per_group = {}
    for name, ids in truth["planted_groups"].items():
        ids = set(ids)
        best = max(flagged, key=lambda g: len(ids & {m["id"] for m in g["members"]}), default=None)
        found = ids & {m["id"] for m in best["members"]} if best else set()
        per_group[name] = {"size": len(ids), "matched_group": best["id"] if best else None,
                           "recall": round(len(found) / len(ids), 2),
                           "signals": best["signal_types"] if best else []}
    tp = len(flagged_ids & planted_ids)
    seed, hop = truth["origin"]["first_post"], truth["origin"]["cross_platform_hop"]
    origin_ok = hop_ok = False
    for o in results["origins"]:
        # the true seed must be the earliest thing the narrative is traced back to
        earliest = o["earlier_sources"][0]["post_id"] if o["earlier_sources"] else \
            (o["first_posts"][0]["post_id"] if o["first_posts"] else None)
        origin_ok |= earliest == seed
        hop_ok |= any(h["from"].get("post_id") == hop["from"] and h["to"]["post_id"] == hop["to"]
                      for h in o["cross_platform_hops"])
    return {
        "precision": round(tp / len(flagged_ids), 3) if flagged_ids else 0.0,
        "recall": round(tp / len(planted_ids), 3),
        "flagged_groups": len(flagged),
        "decoy_accounts_flagged": len(flagged_ids & decoy_ids),
        "per_planted_group": per_group,
        "origin_found": origin_ok,
        "cross_platform_hop_found": hop_ok,
    }
