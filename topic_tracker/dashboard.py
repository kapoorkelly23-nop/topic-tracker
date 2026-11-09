"""Self-contained interactive dashboard (one HTML file, opens in any browser, no server).

Every post, account and piece of evidence links to the original on YouTube or Telegram, so the
numbers can be checked live. Post text comes from the internet, so it is always inserted as text
(never as HTML) and only http(s) links are rendered.
"""
from __future__ import annotations

import json
from pathlib import Path

from .models import Dataset, iso

_FIELDS = ("views", "likes", "comments", "shares", "reactions")


def _safe(url):
    return url if isinstance(url, str) and url.startswith(("https://", "http://")) else None


def build_payload(ds: Dataset, results: dict) -> dict:
    flagged = [g for g in results["groups"] if g["flagged"]]
    group_of = {m["id"]: g["id"] for g in flagged for m in g["members"]}
    used = set(group_of)
    posts = []
    for p in sorted(ds.posts.values(), key=lambda p: p.created_at):
        first = p.engagement_history[0] if len(p.engagement_history) > 1 else None
        posts.append({
            "id": p.id, "pf": p.platform, "k": p.kind, "u": _safe(p.url), "a": p.author_id,
            "t": iso(p.created_at), "x": (p.text or "")[:1500], "m": sorted({m.type for m in p.media}),
            "e": [getattr(p.engagement, f) for f in _FIELDS],
            "e0": [first.get(f) for f in _FIELDS] if first else None,
            "n": results["assignment"].get(p.id), "ok": p.available,
            "fw": bool(p.share_of), "rt": p.reply_to,
        })
        used.add(p.author_id)
    accounts = {}
    for aid in used:
        a = ds.accounts.get(aid)
        accounts[aid] = {"l": a.label() if a else aid, "u": _safe(a.url) if a else None,
                         "c": iso(a.created_at) if a else None, "g": group_of.get(aid),
                         "k": a.kind if a else None}
    narratives = [{k: v for k, v in n.items() if k != "post_ids"} for n in results["narratives"]]
    return {
        "meta": {**ds.meta, "posts": len(posts), "accounts": len(ds.accounts)},
        "posts": posts, "accounts": accounts, "narratives": narratives,
        "groups": results["groups"], "origins": results["origins"],
        "fields": list(_FIELDS),
    }


def write_dashboard(ds: Dataset, results: dict, path: str | Path) -> Path:
    data = json.dumps(build_payload(ds, results), ensure_ascii=False, default=str).replace("</", "<\\/")
    path = Path(path)
    path.write_text(_TEMPLATE.replace("__DATA__", data), encoding="utf-8")
    return path


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Topic tracker</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Public+Sans:wght@400;600;700&family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&display=swap" rel="stylesheet">
<style>
:root{
  color-scheme: light;
  --bg:#F3F5F7; --surface:#FFFFFF; --ink:#18202A; --muted:#5B6676; --line:#D9DEE5; --soft:#E9EDF1;
  --yt:#C23B2E; --tg:#2B86C5; --hl:#F6CD5A; --hl-ink:#18202A; --focus:#2B86C5; --gone:#9AA3AE;
  --up:#2F7D4F; --down:#B23A2E;
  --sans:"Public Sans", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  --serif:"Source Serif 4", Georgia, "Times New Roman", serif;
}
@media (prefers-color-scheme: dark){
  :root{
    color-scheme: dark;
    --bg:#12161B; --surface:#1A2027; --ink:#E7EBF0; --muted:#9AA5B4; --line:#2C343E; --soft:#222932;
    --yt:#E0675A; --tg:#5AAEE6; --hl:#8C6A12; --hl-ink:#FFF3CC; --focus:#5AAEE6; --gone:#66717E;
    --up:#6FCF97; --down:#F08A7E;
  }
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 var(--sans);font-variant-numeric:tabular-nums}
a{color:inherit;text-decoration-thickness:1px;text-underline-offset:2px}
a:hover{text-decoration-thickness:2px}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px;border-radius:3px}
button,select,input{font:inherit;color:inherit}
.wrap{max-width:1240px;margin:0 auto;padding:28px 24px 64px}
header{display:flex;flex-wrap:wrap;justify-content:space-between;gap:12px 32px;align-items:flex-end;padding-bottom:18px;border-bottom:1px solid var(--line)}
h1{font:600 clamp(28px,4vw,40px)/1.1 var(--serif);margin:0 0 6px;letter-spacing:-.01em}
.summary{margin:0;color:var(--muted);max-width:70ch}
.stamp{color:var(--muted);font-size:13px;text-align:right}
nav{display:flex;gap:4px;margin:18px 0 22px;overflow-x:auto}
nav button{background:none;border:0;padding:8px 14px;border-radius:6px;cursor:pointer;color:var(--muted);white-space:nowrap}
nav button[aria-selected="true"]{background:var(--surface);color:var(--ink);box-shadow:inset 0 -2px 0 var(--ink)}
h2{font:600 22px/1.25 var(--serif);margin:0 0 4px}
h3{font-size:16px;margin:0 0 6px}
.note{color:var(--muted);margin:0 0 16px;max-width:75ch}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:18px}
section+section{margin-top:28px}
/* highlighter: the one loud element, marks coordinated accounts everywhere */
mark.g{background:linear-gradient(transparent 48%,var(--hl) 48%,var(--hl) 92%,transparent 92%);color:var(--hl-ink);padding:0 3px;font-weight:700;font-size:12px;border-radius:2px}
.acct.flag{background:linear-gradient(transparent 55%,var(--hl) 55%,var(--hl) 90%,transparent 90%);color:var(--hl-ink)}
.pf{display:inline-flex;align-items:center;gap:6px;font-size:13px;white-space:nowrap}
.pf::before{content:"";width:8px;height:8px;border-radius:50%;background:var(--c)}
.pf.youtube{--c:var(--yt)} .pf.telegram{--c:var(--tg)}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{border:1px solid var(--line);background:var(--surface);padding:4px 10px;border-radius:999px;cursor:pointer;font-size:13px}
.chip[aria-pressed="true"]{background:var(--ink);color:var(--bg);border-color:var(--ink)}
/* chart */
.chart{position:relative}
.chart svg{display:block;width:100%;height:auto}
.chart .tip{position:absolute;pointer-events:none;background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:8px 10px;font-size:12px;min-width:180px;display:none;box-shadow:0 4px 14px rgba(0,0,0,.12)}
.legend{display:flex;flex-wrap:wrap;gap:4px 14px;margin-top:10px;font-size:13px}
.legend button{background:none;border:0;padding:2px 0;cursor:pointer;display:flex;gap:6px;align-items:center;color:var(--muted)}
.legend button[aria-pressed="true"]{color:var(--ink)}
.legend i{width:14px;height:3px;border-radius:2px;background:var(--c)}
.axis{fill:var(--muted);font-size:11px}
/* tables */
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse}
th{font-size:12.5px;color:var(--muted);font-weight:700;text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:10px;border-bottom:1px solid var(--soft);vertical-align:top}
th.num,td.num{text-align:right}
th button{background:none;border:0;padding:0;cursor:pointer;color:inherit;font-weight:inherit}
th button[data-active]::after{content:" \25BE"}
tr.gone td{color:var(--gone)}
.text{max-width:46ch;min-width:24ch}
/* posts table: own scroll area so the sideways scrollbar is always on screen, headings stay put */
.tablebox{max-height:calc(100vh - 240px);min-height:320px;overflow:auto}
.tablebox thead th{position:sticky;top:0;background:var(--surface);z-index:1;box-shadow:0 1px 0 var(--line)}
td.who{min-width:15ch;max-width:22ch;overflow-wrap:break-word}
td.when .pf{display:flex;margin-top:3px}
.eng{font-size:13px;line-height:1.5;white-space:nowrap}
.eng b{font-weight:700;font-size:14px}
.eng .delta{display:inline;margin-left:4px;font-size:12px}
.eng .none{color:var(--muted)}
.text p{margin:0;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.tags{display:flex;gap:6px;margin-top:4px;font-size:12px;color:var(--muted)}
.delta{display:block;font-size:12px}
.delta.up{color:var(--up)} .delta.down{color:var(--down)}
.open{white-space:nowrap;font-weight:700}
.time{white-space:nowrap;color:var(--muted);font-size:13px}
.split{display:flex;height:6px;border-radius:3px;overflow:hidden;background:var(--soft);min-width:70px}
.split span{background:var(--c)}
.spark{display:block}
.filters{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:14px}
.filters input[type=search]{flex:1 1 240px;padding:8px 10px;border:1px solid var(--line);border-radius:6px;background:var(--surface)}
.filters select{padding:7px 8px;border:1px solid var(--line);border-radius:6px;background:var(--surface);max-width:100%;min-width:0;flex:0 1 auto}
.filters label{display:flex;gap:6px;align-items:center;font-size:14px}
.pager{display:flex;gap:10px;align-items:center;justify-content:flex-end;margin-top:12px;color:var(--muted);font-size:14px}
.pager button,.btn{border:1px solid var(--line);background:var(--surface);padding:6px 12px;border-radius:6px;cursor:pointer}
.pager button:disabled{opacity:.4;cursor:default}
blockquote{margin:6px 0 0;font:15.5px/1.55 var(--serif);max-width:68ch}
.quotes{list-style:none;padding:0;margin:10px 0 0;display:grid;gap:12px}
.quotes .who{font-size:13px;color:var(--muted)}
details>summary{cursor:pointer;list-style:none}
details>summary::-webkit-details-marker{display:none}
details>summary::before{content:"\25B8";display:inline-block;width:1em;color:var(--muted);transition:transform .15s}
details[open]>summary::before{transform:rotate(90deg)}
@media (prefers-reduced-motion: reduce){details>summary::before{transition:none}}
/* groups */
.group.panel{border-left:4px solid var(--hl);padding:18px 18px 18px 20px}
.group+.group{margin-top:28px}
.conf{font-size:13px;color:var(--muted)}
.reasons{margin:12px 0;padding:0;list-style:none;display:grid;gap:10px}
.reasons>li{padding:10px 12px;background:var(--soft);border-radius:6px}
.reasons strong{display:block;margin-bottom:2px}
.evidence{margin:6px 0 0;padding-left:18px;font-size:13.5px;color:var(--muted)}
.evidence li+li{margin-top:2px}
.members{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:14px}
/* origins timeline (a real sequence) */
.timeline{list-style:none;margin:12px 0 0;padding:0 0 0 22px;border-left:2px solid var(--line)}
.timeline li{position:relative;padding:0 0 14px}
.timeline li::before{content:"";position:absolute;left:-28px;top:6px;width:10px;height:10px;border-radius:50%;background:var(--surface);border:2px solid var(--c,var(--muted))}
.timeline .kind{font-size:12.5px;color:var(--muted)}
.empty{color:var(--muted);padding:24px 0}
@media (max-width:720px){.wrap{padding:18px 14px 48px}.stamp{text-align:left}td,th{padding:8px 6px}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div><h1 id="topic"></h1><p class="summary" id="summary"></p></div>
    <div class="stamp" id="stamp"></div>
  </header>
  <nav role="tablist" aria-label="Sections">
    <button role="tab" data-tab="overview">Narratives</button>
    <button role="tab" data-tab="posts">Posts</button>
    <button role="tab" data-tab="groups">Coordinated groups</button>
    <button role="tab" data-tab="origins">Origins and spread</button>
  </nav>
  <main id="view"></main>
</div>
<script type="application/json" id="data">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('data').textContent);
const COLORS = ['#2B6CB0','#C05621','#2F855A','#B83280','#6B46C1','#B7791F','#319795','#718096','#9C4221','#276749'];
const PF = {youtube:'YouTube', telegram:'Telegram'};
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const safe = u => (typeof u === 'string' && /^https?:\/\//i.test(u)) ? u : null;
const fmt = n => n == null ? '–' : n >= 1e6 ? (n/1e6).toFixed(1).replace(/\.0$/,'') + 'M' : n >= 1e4 ? Math.round(n/1e3) + 'k' : n.toLocaleString();
const dt = s => s ? new Date(s).toLocaleString(undefined, {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'}) : '';
const day = s => s ? new Date(s).toLocaleDateString(undefined, {month:'short', day:'numeric', year:'numeric'}) : '';
const link = (u, text, cls='') => safe(u) ? `<a class="${cls}" href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(text)}</a>` : esc(text);
const narr = Object.fromEntries(D.narratives.map((n, i) => [n.id, {...n, color: COLORS[i % COLORS.length]}]));
const postById = Object.fromEntries(D.posts.map(p => [p.id, p]));

function acct(id){
  const a = D.accounts[id] || {l: id};
  const cls = 'acct' + (a.g ? ' flag' : '');
  const name = safe(a.u) ? link(a.u, a.l, cls) : `<span class="${cls}">${esc(a.l)}</span>`;
  return a.g ? `${name} <mark class="g" title="Member of coordinated group ${esc(a.g)}">${esc(a.g)}</mark>` : name;
}
function score(e){ if(!e) return 0; const [v,l,c,s,r] = e; return (l||0)+(c||0)+(s||0)+(r||0)+Math.floor((v||0)/100); }

/* ---------------- header */
const m = D.meta;
document.getElementById('topic').textContent = m.topic || 'Topic report';
document.title = (m.topic || 'Topic') + ' – topic tracker';
const flagged = D.groups.filter(g => g.flagged);
document.getElementById('summary').textContent =
  `${day(m.since)} to ${day(m.until)}. ${m.posts.toLocaleString()} posts from ${m.accounts.toLocaleString()} accounts on ${(m.platforms||[]).map(p => PF[p]||p).join(' and ')}. ` +
  `${D.narratives.length} narratives, ${flagged.length} coordinated group${flagged.length === 1 ? '' : 's'} flagged.`;
document.getElementById('stamp').innerHTML =
  (m.collected_at ? `Collected ${esc(dt(m.collected_at))}<br>` : '') +
  (m.refreshed_at ? `Engagement refreshed ${esc(dt(m.refreshed_at))}` : 'Engagement as collected. Open a post for live numbers.');

/* ---------------- tabs */
const state = {tab: 'overview', pf: 'all', q: '', n: '', g: '', flagOnly: false, gone: false, sort: 'time', dir: -1, page: 0};
const tabs = document.querySelectorAll('nav button');
tabs.forEach(b => b.addEventListener('click', () => show(b.dataset.tab)));
function show(tab, push=true){
  state.tab = tab;
  tabs.forEach(b => b.setAttribute('aria-selected', b.dataset.tab === tab));
  ({overview: renderOverview, posts: renderPosts, groups: renderGroups, origins: renderOrigins})[tab]();
  if (push) history.replaceState(null, '', '#' + tab);
  window.scrollTo({top: 0});
}

/* ---------------- charts */
function days(){
  const out = [], d = new Date(m.since.slice(0,10) + 'T00:00:00Z'), end = new Date(m.until.slice(0,10) + 'T00:00:00Z');
  while (d <= end){ out.push(d.toISOString().slice(0,10)); d.setUTCDate(d.getUTCDate() + 1); }
  return out;
}
const DAYS = days();
function series(n, pf){ return DAYS.map(d => { const v = n.volume[d] || {}; return pf === 'all' ? Object.values(v).reduce((a,b) => a+b, 0) : (v[pf] || 0); }); }

function lineChart(el, rows){
  const W = 1000, H = 300, L = 36, R = 10, T = 12, B = 28;
  const max = Math.max(1, ...rows.flatMap(r => r.values));
  const x = i => L + (W - L - R) * (DAYS.length < 2 ? 0 : i / (DAYS.length - 1));
  const y = v => T + (H - T - B) * (1 - v / max);
  const ticks = [0, Math.ceil(max/2), max];
  const step = Math.max(1, Math.ceil(DAYS.length / 8));
  let s = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Posts per day by narrative">`;
  ticks.forEach(t => s += `<line x1="${L}" x2="${W-R}" y1="${y(t)}" y2="${y(t)}" stroke="var(--soft)"/><text class="axis" x="${L-6}" y="${y(t)+4}" text-anchor="end">${t}</text>`);
  DAYS.forEach((d, i) => { if (i % step === 0) s += `<text class="axis" x="${x(i)}" y="${H-8}" text-anchor="middle">${new Date(d+'T00:00:00Z').toLocaleDateString(undefined,{month:'short',day:'numeric',timeZone:'UTC'})}</text>`; });
  rows.forEach(r => s += `<polyline data-id="${r.id}" fill="none" stroke="${r.color}" stroke-width="2" stroke-linejoin="round" points="${r.values.map((v,i) => x(i)+','+y(v)).join(' ')}"/>`);
  s += `<line class="cursor" y1="${T}" y2="${H-B}" stroke="var(--muted)" stroke-dasharray="3 3" visibility="hidden"/><rect x="${L}" y="${T}" width="${W-L-R}" height="${H-T-B}" fill="transparent"/></svg><div class="tip"></div>`;
  el.innerHTML = s;
  const svg = el.querySelector('svg'), tip = el.querySelector('.tip'), cur = el.querySelector('.cursor');
  svg.addEventListener('mousemove', ev => {
    const box = svg.getBoundingClientRect(), px = (ev.clientX - box.left) / box.width * W;
    const i = Math.max(0, Math.min(DAYS.length - 1, Math.round((px - L) / (W - L - R) * (DAYS.length - 1))));
    cur.setAttribute('x1', x(i)); cur.setAttribute('x2', x(i)); cur.setAttribute('visibility', 'visible');
    const vals = rows.map(r => [r, r.values[i]]).filter(([,v]) => v > 0).sort((a,b) => b[1] - a[1]);
    tip.innerHTML = `<strong>${esc(new Date(DAYS[i]+'T00:00:00Z').toLocaleDateString(undefined,{month:'short',day:'numeric',timeZone:'UTC'}))}</strong><br>` +
      (vals.length ? vals.map(([r,v]) => `<span style="color:${r.color}">\u25CF</span> ${esc(r.id)}: ${v}`).join('<br>') : 'No posts');
    tip.style.display = 'block';
    const left = (ev.clientX - box.left) + 14;
    tip.style.left = Math.min(left, box.width - tip.offsetWidth - 4) + 'px';
    tip.style.top = '8px';
  });
  svg.addEventListener('mouseleave', () => { tip.style.display = 'none'; cur.setAttribute('visibility', 'hidden'); });
}
function spark(values, color){
  const W = 120, H = 28, max = Math.max(1, ...values);
  const pts = values.map((v,i) => `${(W * i / Math.max(1, values.length - 1)).toFixed(1)},${(H - 2 - (H - 4) * v / max).toFixed(1)}`).join(' ');
  return `<svg class="spark" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" aria-hidden="true"><polyline fill="none" stroke="${color}" stroke-width="1.5" points="${pts}"/></svg>`;
}

/* ---------------- narratives (overview) */
function renderOverview(){
  const pfs = ['all', ...(m.platforms || [])];
  const coordShare = Object.fromEntries(D.origins.map(o => [o.narrative_id, o.share_of_posts_from_coordinated_groups]));
  const v = document.getElementById('view');
  v.innerHTML = `
    <section class="panel">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:10px;margin-bottom:10px">
        <div><h2>What is being said</h2><p class="note" style="margin:0">Posts per day for each narrative. Hover the chart for daily counts; select a narrative below to focus it.</p></div>
        <div class="chips" role="group" aria-label="Platform">${pfs.map(p => `<button class="chip" data-pf="${p}" aria-pressed="${state.pf === p}">${p === 'all' ? 'All platforms' : PF[p]}</button>`).join('')}</div>
      </div>
      <div class="chart" id="chart"></div>
      <div class="legend" id="legend"></div>
    </section>
    <section class="panel">
      <div class="scroll"><table>
        <thead><tr><th>Narrative</th><th class="num">Posts</th><th class="num">Accounts</th><th>Platforms</th><th>Trend</th><th>First seen</th><th class="num">From flagged groups</th><th></th></tr></thead>
        <tbody>${D.narratives.map(n => {
          const c = narr[n.id].color, yt = n.platforms.youtube || 0, tg = n.platforms.telegram || 0, share = coordShare[n.id] || 0;
          return `<tr>
            <td><details><summary><strong style="color:${c}">${esc(n.id)}</strong> ${esc(n.top_terms.slice(0,4).join(', '))}</summary>
              <ul class="quotes">${n.representative_posts.map(r => `<li><div class="who"><span class="pf ${esc(r.platform)}">${esc(PF[r.platform]||r.platform)}</span> ${acct(postById[r.post_id]?.a || '')} ${link(r.url, 'Open post', 'open')}</div><blockquote>${esc(r.text)}</blockquote></li>`).join('')}</ul></details></td>
            <td class="num">${n.posts}</td><td class="num">${n.accounts}</td>
            <td><div class="split" title="YouTube ${yt}, Telegram ${tg}"><span style="--c:var(--yt);width:${100*yt/n.posts}%"></span><span style="--c:var(--tg);width:${100*tg/n.posts}%"></span></div></td>
            <td>${spark(series(n, state.pf), c)}</td>
            <td class="time">${esc(dt(n.first_seen))}</td>
            <td class="num">${share ? `<mark class="g">${Math.round(share*100)}%</mark>` : '0%'}</td>
            <td><button class="btn" data-posts="${esc(n.id)}">See posts</button></td></tr>`; }).join('')}
        </tbody></table></div>
    </section>`;
  let focus = null;
  const draw = () => {
    const rows = D.narratives.slice(0, 10).map(n => ({id: n.id, color: narr[n.id].color, values: series(n, state.pf)}));
    lineChart(document.getElementById('chart'), rows);
    document.querySelectorAll('#chart polyline').forEach(pl => pl.style.opacity = !focus || pl.dataset.id === focus ? 1 : .12);
    document.getElementById('legend').innerHTML = rows.map(r => `<button data-id="${r.id}" aria-pressed="${!focus || focus === r.id}"><i style="--c:${r.color}"></i>${esc(r.id)}: ${esc(narr[r.id].label)}</button>`).join('');
    document.querySelectorAll('#legend button').forEach(b => b.addEventListener('click', () => { focus = focus === b.dataset.id ? null : b.dataset.id; draw(); }));
  };
  draw();
  v.querySelectorAll('[data-pf]').forEach(b => b.addEventListener('click', () => { state.pf = b.dataset.pf; renderOverview(); }));
  v.querySelectorAll('[data-posts]').forEach(b => b.addEventListener('click', () => { Object.assign(state, {n: b.dataset.posts, g: '', q: '', page: 0}); show('posts'); }));
}

/* ---------------- posts */
const PAGE = 50;
function filtered(){
  const q = state.q.trim().toLowerCase();
  let rows = D.posts.filter(p =>
    (state.pf === 'all' || p.pf === state.pf) && (!state.n || p.n === state.n) &&
    (!state.g || D.accounts[p.a]?.g === state.g) && (!state.flagOnly || D.accounts[p.a]?.g) &&
    (!state.gone || !p.ok) &&
    (!q || p.x.toLowerCase().includes(q) || (D.accounts[p.a]?.l || '').toLowerCase().includes(q)));
  const key = {
    time: p => p.t, engagement: p => score(p.e), views: p => p.e[0] ?? -1, likes: p => p.e[1] ?? -1,
    growth: p => p.e0 ? score(p.e) - score(p.e0) : -1,
  }[state.sort];
  return rows.sort((a, b) => (key(a) > key(b) ? 1 : key(a) < key(b) ? -1 : 0) * state.dir);
}
const SORTS = [['newest', ['time', -1], 'Newest first'], ['oldest', ['time', 1], 'Oldest first'],
  ['engagement', ['engagement', -1], 'Most engagement'], ['views', ['views', -1], 'Most views'],
  ['likes', ['likes', -1], 'Most likes'], ['growth', ['growth', -1], 'Fastest growing']];
function sortKey(){ const s = SORTS.find(([, [k, d]]) => k === state.sort && d === state.dir); return s ? s[0] : 'newest'; }
function engagement(p){
  const labels = ['views', 'likes', 'comments', p.pf === 'telegram' ? 'forwards' : 'shares', 'reactions'];
  const lines = labels.map((label, i) => p.e[i] == null ? '' : `<div><b>${fmt(p.e[i])}</b> ${label}${delta(p, i)}</div>`).filter(Boolean);
  return lines.length ? lines.join('') : '<span class="none">No numbers</span>';
}
function delta(p, i){
  const now = p.e[i], before = p.e0 ? p.e0[i] : null;
  if (now == null || before == null || now === before) return '';
  const diff = now - before;
  return `<span class="delta ${diff > 0 ? 'up' : 'down'}" title="Change since collection">${diff > 0 ? '+' : '\u2212'}${fmt(Math.abs(diff))}</span>`;
}
function renderPosts(){
  const v = document.getElementById('view');
  const groupsOpt = flagged.map(g => `<option value="${esc(g.id)}" ${state.g === g.id ? 'selected' : ''}>${esc(g.id)} (${g.size} accounts)</option>`).join('');
  const hasGone = D.posts.some(p => !p.ok), hasHist = D.posts.some(p => p.e0);
  v.innerHTML = `
    <section>
      <h2>Posts</h2>
      <p class="note">Numbers are from ${m.refreshed_at ? 'the last refresh' : 'collection time'}${hasHist ? ', with the change since collection in green' : ''}. Select Open to see the post and its live engagement on the platform. Accounts with a highlighter mark belong to a flagged coordinated group.</p>
      <div class="filters">
        <input type="search" id="q" placeholder="Search text or account" value="${esc(state.q)}" aria-label="Search posts">
        <select id="pf" aria-label="Platform"><option value="all">All platforms</option>${(m.platforms||[]).map(p => `<option value="${p}" ${state.pf === p ? 'selected' : ''}>${PF[p]}</option>`).join('')}</select>
        <select id="n" aria-label="Narrative"><option value="">All narratives</option>${D.narratives.map(n => `<option value="${esc(n.id)}" ${state.n === n.id ? 'selected' : ''}>${esc(n.id)}: ${esc(n.label)}</option>`).join('')}</select>
        <select id="g" aria-label="Coordinated group"><option value="">All accounts</option>${groupsOpt}</select>
        <label><input type="checkbox" id="flag" ${state.flagOnly ? 'checked' : ''}> Flagged groups only</label>
        ${hasGone ? `<label><input type="checkbox" id="gone" ${state.gone ? 'checked' : ''}> Removed posts only</label>` : ''}
        <select id="sort" aria-label="Sort posts">${SORTS.filter(([k]) => hasHist || k !== 'growth').map(([k, , label]) =>
          `<option value="${k}" ${sortKey() === k ? 'selected' : ''}>${label}</option>`).join('')}</select>
      </div>
      <div class="panel tablebox" style="padding:0"><table>
        <thead><tr>
          <th><button data-sort="time">Posted</button></th><th>Account</th><th>Post</th>
          <th>Engagement</th>
          <th class="num"><button data-sort="engagement">Total</button></th>${hasHist ? '<th class="num"><button data-sort="growth">Growth</button></th>' : ''}<th></th>
        </tr></thead><tbody id="rows"></tbody></table></div>
      <div class="pager"><span id="count"></span><button id="prev">Previous</button><button id="next">Next</button></div>
    </section>`;
  const rowsEl = document.getElementById('rows');
  const paint = () => {
    const rows = filtered(), pages = Math.max(1, Math.ceil(rows.length / PAGE));
    state.page = Math.min(state.page, pages - 1);
    const slice = rows.slice(state.page * PAGE, (state.page + 1) * PAGE);
    rowsEl.innerHTML = slice.length ? slice.map(p => {
      const n = p.n ? narr[p.n] : null;
      const tags = [p.k === 'comment' ? 'Comment' : p.k === 'video' ? 'Video' : null, p.fw ? 'Forwarded' : null,
        ...p.m.filter(x => x === 'photo').map(() => 'Photo'), n ? `<span style="color:${n.color}">${esc(n.id)}</span>` : null,
        !p.ok ? '<strong>Removed at last refresh</strong>' : null].filter(Boolean);
      const growth = p.e0 ? score(p.e) - score(p.e0) : null;
      return `<tr class="${p.ok ? '' : 'gone'}">
        <td class="time when">${esc(dt(p.t))}<span class="pf ${esc(p.pf)}">${esc(PF[p.pf] || p.pf)}</span></td>
        <td class="who">${acct(p.a)}</td>
        <td class="text"><p title="${esc(p.x)}">${esc(p.x)}</p><div class="tags">${tags.join('<span aria-hidden="true">/</span>')}</div></td>
        <td class="eng">${engagement(p)}</td><td class="num"><strong>${fmt(score(p.e))}</strong></td>
        ${hasHist ? `<td class="num">${growth == null ? '–' : `<span class="delta ${growth > 0 ? 'up' : growth < 0 ? 'down' : ''}">${growth > 0 ? '+' : ''}${fmt(growth)}</span>`}</td>` : ''}
        <td>${link(p.u, 'Open', 'open')}</td></tr>`;
    }).join('') : `<tr><td colspan="8" class="empty">No posts match these filters. Clear the search or choose All narratives.</td></tr>`;
    document.getElementById('count').textContent = rows.length ? `${state.page * PAGE + 1}\u2013${Math.min(rows.length, (state.page + 1) * PAGE)} of ${rows.length.toLocaleString()}` : '0 posts';
    document.getElementById('prev').disabled = state.page === 0;
    document.getElementById('next').disabled = state.page >= pages - 1;
    v.querySelectorAll('[data-sort]').forEach(b => b.toggleAttribute('data-active', b.dataset.sort === state.sort));
    const sel = document.getElementById('sort'); if (sel) sel.value = sortKey();
  };
  const bind = (id, ev, fn) => { const el = document.getElementById(id); if (el) el.addEventListener(ev, () => { fn(el); state.page = 0; paint(); }); };
  bind('q', 'input', el => state.q = el.value);
  bind('pf', 'change', el => state.pf = el.value);
  bind('n', 'change', el => state.n = el.value);
  bind('g', 'change', el => state.g = el.value);
  bind('flag', 'change', el => state.flagOnly = el.checked);
  bind('gone', 'change', el => state.gone = el.checked);
  bind('sort', 'change', el => { const s = SORTS.find(([k]) => k === el.value); state.sort = s[1][0]; state.dir = s[1][1]; });
  document.getElementById('prev').addEventListener('click', () => { state.page--; paint(); });
  document.getElementById('next').addEventListener('click', () => { state.page++; paint(); });
  v.querySelectorAll('[data-sort]').forEach(b => b.addEventListener('click', () => {
    if (state.sort === b.dataset.sort) state.dir *= -1; else { state.sort = b.dataset.sort; state.dir = -1; }
    paint();
  }));
  paint();
}

/* ---------------- coordinated groups */
const SIGNAL = {text_reuse: 'Same or near-identical text', co_posting: 'Posting within minutes of each other',
  shared_avatar: 'Same profile photo', co_amplification: 'Boosting the same posts',
  mutual_amplification: 'Boosting each other', creation_burst: 'Accounts created around the same time'};
function evidence(r){
  return (r.evidence || []).slice(0, 4).map(e => {
    if (e.paired_with) return `<li>${link(e.url, e.account + ', ' + dt(e.time))} and ${link(e.paired_with.url, e.paired_with.account + ', ' + dt(e.paired_with.time))}: ${e.gap_seconds}s apart</li>`;
    if (e.url && e.text) return `<li>${link(e.url, e.account + ', ' + dt(e.time))}: \u201C${esc(e.text.length > 110 ? e.text.slice(0, 110).trimEnd() + '\u2026' : e.text)}\u201D</li>`;
    if (e.created_at) return `<li>${esc(e.account)} created ${esc(day(e.created_at))}</li>`;
    if (e.hamming_distance != null) return `<li>${esc(e.accounts.join(' and '))}: images ${e.hamming_distance === 0 ? 'identical' : e.hamming_distance + ' bits apart'}</li>`;
    if (e.targets) return `<li>${esc(e.accounts.join(' and '))}: ${e.shared_targets} shared targets, e.g. ${e.targets.map((t, i) => link(t, 'target ' + (i + 1))).join(', ')}</li>`;
    if (e.a_to_b != null) return `<li>${esc(e.accounts[0])} boosted ${esc(e.accounts[1])} ${e.a_to_b}\u00D7, and ${esc(e.accounts[1])} boosted ${esc(e.accounts[0])} ${e.b_to_a}\u00D7</li>`;
    return '';
  }).join('');
}
function groupBlock(g){
  return `<article class="group panel">
    <h3>${esc(g.id)}: ${g.size} accounts</h3>
    <div class="conf">${esc(g.confidence[0].toUpperCase() + g.confidence.slice(1))} confidence, ${g.signal_types.length} independent signal${g.signal_types.length === 1 ? '' : 's'}. ${Object.entries(g.platforms).map(([k,v]) => `${PF[k] || k}: ${v}`).join(', ')}</div>
    <ul class="reasons">${g.reasons.map(r => `<li><strong>${esc(SIGNAL[r.signal] || r.signal)}</strong>${esc(r.summary)}<ul class="evidence">${evidence(r)}</ul></li>`).join('')}</ul>
    <div class="members">${g.members.map(mb => `<span>${link(mb.url, mb.label)}${mb.created_at ? ` <span class="conf">${esc(day(mb.created_at))}</span>` : ''}</span>`).join('')}</div>
    <p style="margin:14px 0 0"><button class="btn" data-group="${esc(g.id)}">See this group's posts</button></p>
  </article>`;
}
function renderGroups(){
  const others = D.groups.filter(g => !g.flagged);
  const v = document.getElementById('view');
  v.innerHTML = `<section>
    <h2>Coordinated groups</h2>
    <p class="note">Accounts that appear to act together. A group is flagged only when at least two independent signals agree, because each signal alone has innocent explanations. Treat these as leads to review, not conclusions.</p>
    ${flagged.length ? flagged.map(groupBlock).join('') : '<p class="empty">No group met the threshold of two independent signals.</p>'}
    ${others.length ? `<details style="margin-top:28px"><summary><strong>${others.length} candidate groups with one signal</strong> <span class="conf">(not flagged)</span></summary><div style="margin-top:14px">${others.map(groupBlock).join('')}</div></details>` : ''}
  </section>`;
  v.querySelectorAll('[data-group]').forEach(b => b.addEventListener('click', () => { Object.assign(state, {g: b.dataset.group, n: '', q: '', page: 0}); show('posts'); }));
}

/* ---------------- origins */
function renderOrigins(){
  const v = document.getElementById('view');
  // open the narrative most driven by coordinated accounts, else the largest
  const lead = D.origins.reduce((best, o, i) => o.share_of_posts_from_coordinated_groups > D.origins[best].share_of_posts_from_coordinated_groups ? i : best, 0);
  v.innerHTML = `<section>
    <h2>Where each narrative started</h2>
    <p class="note">Earliest posts, older sources they point back to, and how each narrative moved between platforms. These are the first <em>observed</em> posts: deleted or uncollected posts may be earlier.</p>
    ${D.origins.map((o, i) => {
      const c = narr[o.narrative_id]?.color || 'var(--muted)';
      const items = [
        ...o.earlier_sources.map(e => ({t: e.time, html: `<span class="kind">Earlier source, found through a ${esc(e.via)}</span><br>${e.post_id && postById[e.post_id] ? acct(postById[e.post_id].a) : esc(e.account)}${e.coordinated_group && !(postById[e.post_id]) ? ` <mark class="g">${esc(e.coordinated_group)}</mark>` : ''} ${link(e.url, 'Open post', 'open')}`, c: 'var(--muted)'})),
        ...o.first_posts.map((f, k) => ({t: f.time, html: `<span class="kind">${k === 0 ? 'First observed post' : 'Early post'}, <span class="pf ${esc(f.platform)}">${esc(PF[f.platform])}</span></span><br>${acct(f.account_id)} ${link(f.url, 'Open post', 'open')}<blockquote>${esc(f.text)}</blockquote>`, c: c})),
        ...o.cross_platform_hops.filter(h => h.via === 'link').slice(0, 3).map(h => ({t: h.to.time, html: `<span class="kind">Jumped from ${esc(PF[h.from_platform])} to ${esc(PF[h.to_platform])} by link${h.lag_hours != null ? `, ${h.lag_hours}h later` : ''}</span><br>${acct(h.to.account_id)} ${link(h.to.url, 'Open post', 'open')} linked to ${h.from.url ? link(h.from.url, 'the source') : 'an uncollected post'}`, c: `var(--${h.to_platform === 'youtube' ? 'yt' : 'tg'})`})),
      ].sort((a, b) => a.t < b.t ? -1 : 1);
      const textHops = o.cross_platform_hops.filter(h => h.via === 'same_text').length;
      const b = o.breakout;
      return `<details class="panel" style="margin-top:14px" ${i === lead ? 'open' : ''}>
        <summary><strong style="color:${c}">${esc(o.narrative_id)}</strong> ${esc(o.label)}
          <span class="conf">${o.platform_path.map(p => PF[p.platform] + (p.lag_hours_from_start ? ` (+${p.lag_hours_from_start}h)` : '')).join(' then ')}${o.share_of_posts_from_coordinated_groups ? `, <mark class="g">${Math.round(o.share_of_posts_from_coordinated_groups*100)}% from flagged groups</mark>` : ''}</span></summary>
        <ol class="timeline">${items.map(it => `<li style="--c:${it.c}"><span class="time">${esc(dt(it.t))}</span><br>${it.html}</li>`).join('')}</ol>
        ${textHops ? `<p class="conf" style="margin:4px 0 0">The same text also reappeared on another platform ${textHops} time${textHops === 1 ? '' : 's'}.</p>` : ''}
        ${b ? `<p style="margin:12px 0 0"><strong>Breakout:</strong> ${b.posts_in_bin} posts in the ${esc(b.bin)} from ${esc(dt(b.spike_start))}, against a usual ${b.baseline_per_bin}. Biggest reach around it: ${b.amplifiers.map(a => acct(a.account_id) + ' ' + link(a.url, '(post)')).join(', ')}.</p>` : ''}
      </details>`;
    }).join('')}
  </section>`;
}

const start = (location.hash || '').slice(1);
show(['overview', 'posts', 'groups', 'origins'].includes(start) ? start : 'overview', false);
</script>
</body>
</html>
"""
