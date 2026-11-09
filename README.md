# topic-tracker

Track a topic (keyword, `#hashtag`, person, brand or event) across **YouTube** and **Telegram** for a date range, and get:

1. **Posts**: text, photos, videos, date, engagement and link (`posts.csv`, and the interactive dashboard).
2. **Narratives**: what is being said, grouped, with volume per day and per platform.
3. **Coordinated groups**: accounts that appear to act together, each with the reasons and evidence.
4. **Origins**: the first accounts and posts behind each narrative, and how it moved between platforms.

Both platforms are free to use.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then fill in the keys below
```

**Older Linux machines** (for example Amazon WorkSpaces on Amazon Linux 2): if the install fails with "Failed to build pillow" or "NumPy requires GCC >= 9.3", the newest versions have no ready-made download for your system. This command makes the installer use versions that do:
```bash
uv pip install -r requirements.txt --only-binary numpy --only-binary scipy --only-binary scikit-learn \
    --only-binary matplotlib --only-binary contourpy --only-binary kiwisolver --only-binary pywavelets --only-binary pillow
```
If the system Python is older than 3.10, install a newer one without admin rights using [uv](https://docs.astral.sh/uv/): `uv python install 3.11`, then `uv venv --python 3.11 .venv`.

**YouTube API key (free, about 2 minutes)**
1. Open the Google Cloud Console, create a project and enable **YouTube Data API v3**.
2. Go to *Credentials → Create credentials → API key* and put it in `.env` as `YOUTUBE_API_KEY`.

The default quota is 10,000 units a day. Each search costs 100 units, so one run uses about 4,000 by default (`--yt-max-search-calls 40`).

**Telegram API ID (free)**
1. Log in at <https://my.telegram.org>, open **API development tools** and create an app.
2. Copy `api_id` and `api_hash` into `.env`.

The first run asks for your phone number and login code in the terminal. After that, the session is saved to `topic_tracker.session`, so keep that file private.

## Search page (easiest)

```bash
python -m topic_tracker serve
```

This opens **http://127.0.0.1:8765** in your browser. On the page you:

1. Type a topic, pick dates, platforms and how thorough the search should be, then click **Search**.
2. Watch the progress while it runs. It takes 1 to 15 minutes, depending on how thorough you chose.
3. The dashboard then opens on the same page.

Every search is saved and listed under **Past searches**. Clicking one reopens it instantly, and **Refresh engagement** updates its numbers. As you type a topic, earlier searches that match it are suggested. The sidebar also shows how much of today's YouTube allowance is used, and when it resets in your local time.

How the page is kept safe and simple:
- **No extra installs:** it uses only Python's built-in web server.
- **Only this machine can reach it:** it listens on 127.0.0.1.
- **Your keys stay private:** API keys stay in `.env`, never in the page.
- **Quota is protected:** one search runs at a time.
- **Other websites can't use it:** they can't trigger searches through your browser.

Press **Ctrl+C** in the terminal to stop the page.

**Telegram on the page:** the page can't ask for a login code. Before using Telegram on the page, log in once in the terminal:
```bash
python -m topic_tracker telegram-login
```

## Usage from the terminal

```bash
# No API keys needed: builds a synthetic dataset with planted networks, analyses it and scores the result
python -m topic_tracker demo --out runs/demo

# Real data: collect and analyse
python -m topic_tracker run --topic "#AcmeBankCollapse" --since 2026-10-01 --until 2026-10-31 \
    --tg-channels channels.example.txt --out runs/acme

# Or run the two steps separately
python -m topic_tracker collect --topic "Acme Bank" --since 2026-10-01 --until 2026-10-31 --out runs/acme
python -m topic_tracker analyze --data runs/acme

# Later: re-fetch current engagement for the same posts and rebuild everything
python -m topic_tracker refresh --data runs/acme
```

Then open `runs/acme/analysis/dashboard.html` in a browser.

Useful options:
- `--platforms youtube` restricts collection to one platform.
- `--tg-channels` takes seed chats as a comma list or a file.
- `--tg-snowball-depth` controls how far collection follows forwards and links.
- `--copost-window 5` sets the "within N minutes" window.
- `--min-signal-types 2` sets how many independent signals a group needs before it's flagged.
- `--embeddings` switches to a multilingual sentence-embedding model (`pip install sentence-transformers`).

## Output (`<out>/analysis/`)

| File | Contents |
|---|---|
| `summary.json` | Short summary that the search page uses for its list of past searches |
| `dashboard.html` | **Interactive dashboard.** One file that opens in any browser with no server. Every post, account and evidence item links to the original. |
| `report.md` | Human-readable report: narratives with charts, flagged groups with reasons, origins and spread |
| `posts.csv` | Every post with text, media links, date, engagement, link and narrative ID |
| `narratives.json` | Key terms, representative posts, daily volume per platform, first seen and peak day |
| `groups.json` | All candidate groups, with members, confidence, reasons and evidence posts (flagged or not) |
| `origins.json` | First posts, earlier sources, platform path, cross-platform hops and breakout amplifiers |
| `charts/*.png` | Volume over time, overall and per narrative |

## Dashboard and live engagement

The dashboard has four tabs:

- **Narratives** shows the volume chart (hover for daily counts, filter by platform). Under it, each narrative has a trend line, its platform split, the share of its posts that came from flagged groups, and representative posts you can expand.
- **Posts** lists every post. You can search, filter by platform, narrative or group, and sort by date, views, likes, total engagement or growth. Every row has an **Open** link to the post itself.
- **Coordinated groups** lists each group's reasons with linked evidence posts and member profiles.
- **Origins and spread** is a timeline per narrative: earlier sources, first posts, cross-platform jumps and the breakout.

Accounts that belong to a flagged group carry a highlighter mark wherever they appear.

**Engagement numbers are a snapshot**, taken when the posts were collected. There are two ways to see current numbers:

1. **Open a post.** Its link shows the live numbers on YouTube or Telegram.
2. **Run `refresh`.** It re-fetches views, likes, comments, forwards and reactions for every collected post, keeps the original numbers as history, and rebuilds the dashboard. The dashboard then shows each post's change since collection and can sort by growth.

Refresh is cheap on YouTube, at 1 quota unit per 50 posts. It also marks posts that have been **deleted or made private** since collection, and you can filter for those. Disappearing posts are worth checking, because coordinated accounts often delete content once it has spread.

Post text comes from the internet, so the dashboard only ever inserts it as plain text and only renders http(s) links. A test checks this.

## How it works

```
connectors/ (YouTube, Telegram) ──> Dataset (common Post / Account model, JSONL)
                                      │
            ┌─────────────────────────┼──────────────────────────┐
     narratives.py             coordination.py               origins.py
```

**Collection.** Both connectors write the same `Post` and `Account` model, so the analysis code never touches platform-specific data. Adding a platform means writing one class.

- **YouTube** searches with the topic's variants (`#AcmeBankCollapse`, `AcmeBankCollapse`, `Acme Bank Collapse`). A search returns at most about 500 results, so busy date windows are split in half until each fits. Earlier windows are searched first, so a tight quota still captures origins. The connector then fetches video stats, comments and replies, and channel details including creation date.
- **Telegram** has no global post search. The connector searches inside chats instead: your seed chats, plus public channels whose names match the topic, plus channels those chats forward from or link to. Forward headers record the original channel, post and time, which is what makes origin tracing work on Telegram.

**Narratives.** The pipeline:
1. Collapses identical texts so one message repeated 500 times doesn't dominate.
2. Removes the topic words. Every post mentions them, so leaving them in hides what is actually being said.
3. Vectorises the text with TF-IDF and SVD.
4. Clusters with HDBSCAN.
5. Attaches outliers to the nearest narrative and merges near-duplicate narratives.
6. Labels each narrative with class-based TF-IDF terms and its most typical posts.

A message repeated by many accounts that fits no existing narrative becomes a narrative of its own.

**Coordination.** Each signal adds evidence edges between pairs of accounts:

| Signal | Method |
|---|---|
| Near-identical text | MinHash LSH over character 5-grams, Jaccard ≥ 0.8. Language-agnostic. Forwards are excluded. |
| Posting within minutes | Same text, link, forwarded post or comment target within the window, counted per distinct occasion. A pair needs at least 3 occasions. |
| Reused profile photo | Perceptual hash, Hamming distance ≤ 6, found with banded LSH. Hashes shared by more than 50 accounts are treated as default avatars and ignored. Photos are only downloaded for accounts that another signal has already linked (see Speed). |
| Same accounts amplifying | Co-amplification: the same forward or comment targets, Jaccard ≥ 0.5 and at least 3 shared. Mutual amplification: A amplifies B and B amplifies A. |
| Accounts created together | Checked at group level: at least 3 members, and at least half of those with known dates, created within 7 days. |

The edges are combined into a weighted graph, and Louvain finds the communities. A community is **flagged only if at least two independent signals each cover a meaningful share of its members**. Any single signal has innocent explanations: copypasta, fan communities, news syndication or default avatars. Every reason links to example posts so an analyst can check it.

**Origins.** For each narrative, the tracer reports:
- the earliest posts from distinct accounts;
- older posts the narrative points back to through forward headers or links, even when they fall outside the cluster or were never collected;
- the first appearance on each platform and the lag between them;
- explicit cross-platform hops, either a link to the other platform or the same text reappearing there;
- the first volume spike and the high-reach accounts around it.

Accounts that belong to flagged groups are tagged wherever they appear.

## Evaluation

Real data has no answer key, so `demo` plants known behaviour in a synthetic October 2026 dataset of about 950 posts and checks whether the pipeline finds it.

The planted behaviour:
- **Network A** is 12 Telegram accounts posting near-identical messages in bursts under 3 minutes. Five of them share a profile photo with small edits, and they forward a seed channel and each other.
- **Network B** is 8 YouTube channels created within 3 days, each posting the same comment within 3 minutes on the same 4 videos.

The traps:
- A viral copypasta posted by 30 unrelated accounts over 10 days, which should not be flagged.
- A news link shared by about 50 unrelated accounts.
- Accidental duplicate texts between organic users.

The ground truth also includes the cover-up narrative's true seed post and its jump from Telegram to YouTube.

| Metric (seeds 1, 2, 3, 4, 5, 11, 23) | Result |
|---|---|
| Precision / recall of flagged accounts | 1.0 / 1.0 on every seed |
| Decoy accounts flagged | 0 |
| True seed post identified as the narrative's origin | yes |
| Telegram → YouTube hop found | yes |

The planted networks are fairly blatant, so these scores show that the logic works. They don't show how it performs in the wild. Real operations use paraphrasing, staggered timing and aged accounts, so the thresholds should be calibrated on labelled real cases before anyone trusts the numbers.

`pytest -q` runs 17 tests. The connector tests use recorded-style API responses and real Telethon types, so they need no keys or network access.

## Speed

On a real run (World Cup, 300 videos, 14,768 posts, 12,353 accounts), most of the time used to go on downloading every commenter's profile photo. Three changes cut that:

- **Photos are checked only for accounts that are already linked** by another signal, such as the same text or posting within minutes. A group needs two independent signals to be flagged, so a photo match on its own could never flag anyone. That run needed about 2,100 photo checks instead of 12,353.
- **Downloads run in parallel:** comments for 8 videos at a time, channel details in parallel, and 32 photo downloads at a time.
- **Photo fingerprints are cached** in `~/.cache/topic_tracker/avatars.json`, so accounts seen in earlier searches aren't downloaded again.

None of this uses extra YouTube quota: photo downloads don't count, and the number of API calls is the same. The trade-off is that an account linked to a group *only* by its profile photo is no longer added to that group. A photo is the weakest signal, so that's an acceptable loss.

## Limitations

- **Telegram coverage** depends on the seed chats and on discovery, because there is no global search. Private chats are out of scope.
- **Telegram never exposes user account creation dates**, so the creation signal works for YouTube and Telegram channels only. For channels, the date is reliable only when your account is not a member of the channel.
- **YouTube** search is relevance-filtered and capped by quota. Use `--yt-max-search-calls` and run collection in date slices for long ranges.
- **"First post"** means the first *observed* post. Deleted or uncollected posts may be earlier.
- **Coordination output is a lead, not a verdict.** It means accounts appear to act together and needs analyst review before any attribution.
- **Narrative labels** are keyword-based. An LLM summary per narrative would read better and is an easy addition, since representative posts are already selected.

## Data handling

The tool collects only public posts and stores only the fields the analysis needs. Collected data includes personal data, so keep runs local and apply a retention limit. India's DPDP Act (and GDPR for EU users) applies to it.

## Project layout

```
topic_tracker/
  models.py          Post, Account, Media, Engagement, Dataset (JSONL persistence)
  text.py            topic variants, normalisation, link parsing
  images.py          perceptual hashing
  connectors/        base.py, youtube.py, telegram.py
  narratives.py      clustering, labels, volume
  coordination.py    signals, graph, groups and reasons
  origins.py         first posts, earlier sources, spread, breakout
  report.py          JSON, CSV, charts, Markdown
  dashboard.py       interactive HTML dashboard
  server.py          search page (python -m topic_tracker serve)
  pipeline.py        collect / analyze / refresh
  synthetic.py       synthetic world with ground truth + evaluator
  cli.py             command line
tests/test_tracker.py
```
