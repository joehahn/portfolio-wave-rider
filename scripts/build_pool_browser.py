#!/usr/bin/env python3
"""Build docs/pool_browser.html — a browsable, per-week view of exactly what the curator receives:
each rebalance's ranked article list with title -> url, date . source . authority tier, the effective
lede (fuller mode: Wayback clean, else live-fallback), and a Wayback / live URL / title-only badge.

Self-contained: the pool data is embedded as JSON and rendered client-side (week dropdown + lede-source
filter), so it works on the public Pages site (unlike the raw-JSON 'inspect' links, which 404 there).

Usage: python scripts/build_pool_browser.py [--run-dir data/curator_runs/gkg-2yr-weekly]
"""
import argparse
import glob
import html
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import gkg_pool as g  # noqa: E402


def _tier(source: str) -> str:
    src = (source or "").lower()
    if g._domain_in(src, g.PREFERRED_DOMAINS):
        return "specialty"
    if g._domain_in(src, g.MAJOR_DOMAINS):
        return "major"
    return "other"


def build(run_rel: str, out: Path) -> None:
    weeks = {}
    for f in sorted(glob.glob(str(ROOT / run_rel / "2*-pool.json"))):
        d = json.loads(Path(f).read_text())
        rows = []
        for a in d.get("articles", []):
            src = a.get("lede_source", "wayback" if a.get("lede") else "none")
            lede = a.get("lede") or a.get("lede_live", "")     # effective (fuller) lede
            rows.append({
                "t": a.get("title", "")[:140], "u": a.get("url", ""),
                "d": a.get("date", ""), "s": a.get("source", ""),
                "tier": _tier(a.get("source", "")),
                "l": (lede or "")[:220], "src": src,
            })
        weeks[d.get("as_of_date", "")] = rows

    data_json = json.dumps(weeks, separators=(",", ":"))
    week_opts = "".join(f'<option value="{w}">{w}</option>' for w in sorted(weeks))
    total = sum(len(v) for v in weeks.values())

    page = f"""<!doctype html><html><head><meta charset="utf-8">
<title>PWR — curator pool browser</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;max-width:1180px;margin:0 auto;
padding:0 1.5em;color:#222;line-height:1.45}}h1{{color:#111}}
nav{{font-size:14px;color:#555;margin:0 0 1em;padding-bottom:.5em;border-bottom:1px solid #eee}}
.ctrl{{margin:1em 0;font-size:14px}}select{{font-size:14px;padding:3px}}
table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top}}
th{{position:sticky;top:0;background:#fff;border-bottom:2px solid #ddd;cursor:default}}
.badge{{font-size:11px;font-weight:600;padding:1px 6px;border-radius:4px;white-space:nowrap}}
.wayback{{background:#d3f9d8;color:#2b8a3e}}.live{{background:#ffe8cc;color:#d9480f}}
.none{{background:#f1f3f5;color:#868e96}}
.tier{{font-size:11px;color:#868e96}}.lede{{color:#495057}}
.summ{{color:#555;font-size:13px;margin:.3em 0 1em}}
</style></head><body>
<nav><a href="https://github.com/joehahn/portfolio-wave-rider/blob/main/README.md">README</a>
 &middot; <a href="retrieval_pwr.html">Retriever DB</a>
 &middot; <a href="backtest_gkg_3yr_kimi.html">Curator DB</a>
 &middot; <a href="forward_test.html">Forward test</a>
 &middot; <a href="sweep_pwr.html">Sweep DB</a></nav>
<h1>Curator pool browser</h1>
<p class="summ">Exactly what the curator receives each rebalance: the ranked article list (title, source,
effective lede, and where the lede came from). <b>Effective lede</b> = clean Wayback snapshot if present,
else the look-ahead-BIASED live-fallback. Badge: <span class="badge wayback">Wayback</span> clean /
<span class="badge live">live URL</span> biased / <span class="badge none">title only</span>.
{len(weeks)} weeks, {total:,} article rows.</p>
<div class="ctrl">
Week: <select id="wk">{week_opts}</select>
&nbsp;&nbsp;Lede source:
<label><input type="radio" name="f" value="all" checked> all</label>
<label><input type="radio" name="f" value="wayback"> Wayback</label>
<label><input type="radio" name="f" value="live"> live URL</label>
<label><input type="radio" name="f" value="none"> title only</label>
<span id="cnt" style="color:#868e96"></span>
</div>
<table><thead><tr><th>#</th><th>date &middot; source</th><th>title / lede</th><th>lede</th></tr></thead>
<tbody id="tb"></tbody></table>
<script>
const DATA = {data_json};
const esc = s => (s||'').replace(/[&<>"]/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]));
function render() {{
  const wk = document.getElementById('wk').value;
  const f = document.querySelector('input[name=f]:checked').value;
  const rows = (DATA[wk]||[]).filter(r => f==='all' || r.src===f);
  const badge = s => `<span class="badge ${{s==='wayback'?'wayback':s==='live'?'live':'none'}}">`
      + (s==='wayback'?'Wayback':s==='live'?'live URL':'title only') + `</span>`;
  document.getElementById('cnt').textContent = ` — ${{rows.length}} shown`;
  document.getElementById('tb').innerHTML = rows.map((r,i) =>
    `<tr><td>${{i+1}}</td><td>${{esc(r.d)}}<br><span class="tier">${{esc(r.s)}} &middot; ${{r.tier}}</span></td>`
    + `<td><a href="${{esc(r.u)}}" target="_blank" rel="noopener">${{esc(r.t)}}</a>`
    + (r.l ? `<div class="lede">${{esc(r.l)}}</div>` : '') + `</td>`
    + `<td>${{badge(r.src)}}</td></tr>`).join('');
}}
document.getElementById('wk').addEventListener('change', render);
document.querySelectorAll('input[name=f]').forEach(el => el.addEventListener('change', render));
render();
</script></body></html>"""
    out.write_text(page)
    print(f"wrote {out}  ({len(weeks)} weeks, {total:,} rows, {len(data_json)//1024} KB embedded)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", default="data/curator_runs/gkg-2yr-weekly")
    ap.add_argument("--out", default=str(ROOT / "docs" / "pool_browser.html"))
    a = ap.parse_args()
    build(a.run_dir, Path(a.out))
