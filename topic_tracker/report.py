"""Write results: JSON for machines, CSV of posts, PNG charts and a Markdown report for people."""
from __future__ import annotations

import csv
import json
import warnings
from datetime import timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
# Posts in other scripts (Hindi, fancy Unicode) only affect chart labels; the dashboard renders them fine.
warnings.filterwarnings("ignore", message="Glyph .* missing from font")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

from .dashboard import write_dashboard  # noqa: E402
from .models import Dataset, iso, to_utc  # noqa: E402


def _days(ds: Dataset, narratives: list[dict]) -> list[str]:
    since, until = to_utc(ds.meta.get("since")), to_utc(ds.meta.get("until"))
    if not since:
        all_days = sorted({d for n in narratives for d in n["volume"]})
        if not all_days:
            return []
        since, until = to_utc(all_days[0]), to_utc(all_days[-1])
    out, d = [], since.date()
    while d <= until.date():
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _charts(ds, narratives, out: Path) -> dict[str, str]:
    days = _days(ds, narratives)
    if not days:
        return {}
    (out / "charts").mkdir(exist_ok=True)
    files = {}
    x = [to_utc(d) for d in days]

    def fmt(ax):
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
        ax.tick_params(axis="x", rotation=45, labelsize=7)

    fig, ax = plt.subplots(figsize=(11, 4.5))
    for n in narratives[:8]:
        ax.plot(x, [sum(n["volume"].get(d, {}).values()) for d in days], label=f"{n['id']}: {n['label'][:40]}")
    ax.set_title("Posts per day by narrative")
    ax.legend(fontsize=7, loc="upper right")
    fmt(ax)
    fig.tight_layout()
    fig.savefig(out / "charts" / "overview.png", dpi=120)
    plt.close(fig)
    files["overview"] = "charts/overview.png"

    for n in narratives[:15]:
        platforms = sorted(n["platforms"])
        fig, ax = plt.subplots(figsize=(9, 3))
        for pl in platforms:
            ax.plot(x, [n["volume"].get(d, {}).get(pl, 0) for d in days], label=pl)
        ax.set_title(f"{n['id']}: {n['label']}", fontsize=10)
        ax.legend(fontsize=8)
        fmt(ax)
        fig.tight_layout()
        fig.savefig(out / "charts" / f"{n['id']}.png", dpi=110)
        plt.close(fig)
        files[n["id"]] = f"charts/{n['id']}.png"
    return files


def _posts_csv(ds: Dataset, assignment: dict, path: Path) -> None:
    cols = ["post_id", "platform", "kind", "created_at", "account", "account_url", "narrative", "text",
            "url", "photos", "videos", "views", "likes", "comments", "shares", "reactions",
            "available", "reply_to", "share_of", "matched_query"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for p in sorted(ds.posts.values(), key=lambda p: p.created_at):
            e = p.engagement
            acc = ds.accounts.get(p.author_id)
            w.writerow([p.id, p.platform, p.kind, iso(p.created_at), ds.account_label(p.author_id),
                        acc.url if acc else "",
                        assignment.get(p.id, ""), p.text, p.url,
                        " ".join(m.url or "" for m in p.media if m.type in ("photo", "thumbnail")),
                        " ".join(m.url or "" for m in p.media if m.type == "video"),
                        e.views, e.likes, e.comments, e.shares, e.reactions, p.available, p.reply_to, p.share_of,
                        p.matched_query])


def _md_escape(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ")


def write_report(ds: Dataset, results: dict, out: str | Path) -> Path:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    narratives, groups, origins = results["narratives"], results["groups"], results["origins"]
    for name in ("narratives", "groups", "origins"):
        (out / f"{name}.json").write_text(json.dumps(results[name], indent=2, ensure_ascii=False, default=str))
    _posts_csv(ds, results["assignment"], out / "posts.csv")
    charts = _charts(ds, narratives, out)
    write_dashboard(ds, results, out / "dashboard.html")
    platforms = {}
    for p in ds.posts.values():
        platforms[p.platform] = platforms.get(p.platform, 0) + 1
    (out / "summary.json").write_text(json.dumps({
        "topic": ds.meta.get("topic"), "since": ds.meta.get("since"), "until": ds.meta.get("until"),
        "collected_at": ds.meta.get("collected_at"), "refreshed_at": ds.meta.get("refreshed_at"),
        "posts": len(ds.posts), "accounts": len(ds.accounts), "platforms": platforms,
        "narratives": len(narratives), "flagged_groups": sum(1 for g in groups if g["flagged"]),
    }, indent=2))

    L = []
    meta = ds.meta
    L.append(f"# Topic report: {meta.get('topic', '')}")
    L.append(f"\nDate range: {meta.get('since', '?')} to {meta.get('until', '?')}  ")
    pc = {}
    for p in ds.posts.values():
        pc[p.platform] = pc.get(p.platform, 0) + 1
    L.append(f"Posts collected: {len(ds.posts)} ({', '.join(f'{k}: {v}' for k, v in sorted(pc.items()))}); "
             f"accounts: {len(ds.accounts)}  ")
    flagged = [g for g in groups if g["flagged"]]
    L.append(f"Narratives: {len(narratives)}; coordinated groups flagged: {len(flagged)}  ")
    L.append("Interactive version with links to every post: open `dashboard.html` in a browser.\n")

    L.append("## Narratives\n")
    if "overview" in charts:
        L.append(f"![overview]({charts['overview']})\n")
    L.append("| ID | Narrative (key terms) | Posts | Accounts | Platforms | First seen | Peak day |")
    L.append("|---|---|---|---|---|---|---|")
    for n in narratives:
        L.append(f"| {n['id']} | {_md_escape(', '.join(n['top_terms'][:5]))} | {n['posts']} | {n['accounts']} | "
                 f"{', '.join(f'{k} {v}' for k, v in n['platforms'].items())} | {n['first_seen'][:16]} | "
                 f"{n['peak_day']} ({n['peak_day_posts']}) |")
    L.append(f"\nUnclustered posts: {results['unclustered'].get('posts', 0)}\n")
    for n in narratives:
        L.append(f"### {n['id']}: {n['label']}\n")
        if n["id"] in charts:
            L.append(f"![{n['id']}]({charts[n['id']]})\n")
        L.append("Representative posts:\n")
        for r in n["representative_posts"]:
            L.append(f"- [{r['platform']}] {r['account']}: \"{_md_escape(r['text'][:200])}\" ([link]({r['url']}))")
        L.append("")

    L.append("## Coordinated activity\n")
    L.append("Groups are flagged only when at least two independent signals agree. "
             "This indicates accounts that *appear* to act together; it needs analyst review.\n")
    if not flagged:
        L.append("No groups met the threshold.\n")
    for g in flagged:
        L.append(f"### {g['id']}: {g['size']} accounts, {g['confidence']} confidence\n")
        L.append(f"Platforms: {', '.join(f'{k} {v}' for k, v in g['platforms'].items())}  ")
        L.append("Members: " + ", ".join(m["label"] for m in g["members"][:20])
                 + (" ..." if g["size"] > 20 else "") + "\n")
        L.append("Why this group was flagged:\n")
        for r in g["reasons"]:
            L.append(f"- **{r['signal']}**: {_md_escape(r['summary'])}")
        L.append("")
    low = [g for g in groups if not g["flagged"]]
    if low:
        L.append(f"{len(low)} further candidate groups had only one signal and were not flagged "
                 "(see groups.json).\n")

    L.append("## Origins and spread\n")
    for o in origins:
        L.append(f"### {o['narrative_id']}: {o['label']}\n")
        L.append("First accounts to push it:\n")
        for f in o["first_posts"]:
            tag = f" **[{f['coordinated_group']}]**" if f["coordinated_group"] else ""
            L.append(f"- {f['time'][:16]} [{f['platform']}] {f['account']}{tag}: "
                     f"\"{_md_escape(f['text'][:120])}\" ([link]({f['url']}))")
        if o["earlier_sources"]:
            L.append("\nEarlier sources this narrative points back to (via forwards or links):\n")
            for e in o["earlier_sources"]:
                tag = f" **[{e['coordinated_group']}]**" if e.get("coordinated_group") else ""
                L.append(f"- {e['time'][:16]} {e['account']}{tag} via {e['via']}: {e['url'] or e['post_id']}")
        L.append("\nPlatform path: " + " -> ".join(
            f"{p['platform']} ({p['first_seen'][:16]}, +{p['lag_hours_from_start']}h)" for p in o["platform_path"]))
        if o["cross_platform_hops"]:
            L.append("\nCross-platform hops:\n")
            for h in o["cross_platform_hops"][:6]:
                src = h["from"].get("account") or h["from"].get("url")
                lag = f", {h['lag_hours']}h later" if h["lag_hours"] is not None else ""
                L.append(f"- {h['from_platform']} -> {h['to_platform']} via {h['via']}: {src} -> "
                         f"{h['to']['account']}{lag}")
        if o["breakout"]:
            b = o["breakout"]
            L.append(f"\nBreakout: {b['posts_in_bin']} posts in the {b['bin']} from {b['spike_start'][:16]} "
                     f"(baseline {b['baseline_per_bin']}). Top amplifiers: "
                     + ", ".join(a["account"] + (f" [{a['coordinated_group']}]" if a["coordinated_group"] else "")
                                 for a in b["amplifiers"]))
        L.append(f"\nShare of posts from flagged coordinated groups: "
                 f"{o['share_of_posts_from_coordinated_groups']:.0%}\n")
    L.append("---\n*First post = first observed post. Deleted or uncollected posts may be earlier.*")
    path = out / "report.md"
    path.write_text("\n".join(L), encoding="utf-8")
    return path
