#!/usr/bin/env python3
"""Build the graveyard from data/graveyard.yml.

Regenerates README.md, README.zh-CN.md and docs/assets/timeline-*.svg.
Text in the timeline is converted to outlines so it renders identically everywhere.
Fonts (Source Serif 4 and Noto Serif SC, both SIL OFL) are fetched once into
scripts/.fonts with `npm pack`.

    pip install -r scripts/requirements.txt   # Python 3.9+, plus Node/npm for the fonts
    python scripts/build.py
"""
from __future__ import annotations

import glob
import io
import math
import hashlib
import html
import os
import re
import subprocess
import sys
import tarfile

import uharfbuzz as hb
import yaml
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT_DIR = os.environ.get("GRAVEYARD_FONTS", os.path.join(ROOT, "scripts", ".fonts"))
PAGE = "https://gulucaptain.github.io/Awesome-Last-Human-Made/"
REPO = "gulucaptain/Awesome-Last-Human-Made"
NOW = 2026

THEMES = {
    "light": {"ink": "#1f2328", "muted": "#6e7781", "hair": "#d8dee4"},
    "dark": {"ink": "#e6edf3", "muted": "#8d96a0", "hair": "#30363d"},
}

# ---------------------------------------------------------------- fonts → outlines
PACKAGES = {"@fontsource/source-serif-4": "source-serif-4", "@fontsource/inter": "inter", "@fontsource/noto-serif-sc": "noto-serif-sc"}


def ensure_fonts():
    for pkg, name in PACKAGES.items():
        dest = os.path.join(FONT_DIR, name)
        if os.path.isdir(os.path.join(dest, "package", "files")):
            continue
        os.makedirs(dest, exist_ok=True)
        print(f"fetching {pkg} …", file=sys.stderr)
        out = subprocess.run(["npm", "pack", pkg, "--silent"], cwd=dest, check=True,
                             capture_output=True, text=True).stdout.strip().splitlines()[-1]
        with tarfile.open(os.path.join(dest, out)) as t:
            try:
                t.extractall(dest, filter="data")
            except TypeError:  # Python < 3.12
                t.extractall(dest)


class Face:
    def __init__(self, path):
        tt = TTFont(path)
        tt.flavor = None
        buf = io.BytesIO()
        tt.save(buf)
        self.tt = TTFont(io.BytesIO(buf.getvalue()))
        self.hb = hb.Font(hb.Face(buf.getvalue()))
        self.upem = self.tt["head"].unitsPerEm
        self.glyphs = self.tt.getGlyphSet()
        self.order = self.tt.getGlyphOrder()


class Family:
    def __init__(self, pattern):
        self.paths = sorted(glob.glob(pattern))
        if not self.paths:
            raise SystemExit(f"no font files match {pattern}")
        self.cover, self.faces = None, {}

    def face_for(self, ch):
        if self.cover is None:
            self.cover = {}
            for p in self.paths:
                for cp in TTFont(p, lazy=True).getBestCmap():
                    self.cover.setdefault(cp, p)
        p = self.cover.get(ord(ch))
        if p and p not in self.faces:
            self.faces[p] = Face(p)
        return self.faces.get(p)


_FAM = {}


def fam(key):
    if not _FAM:
        f = lambda pkg, pat: os.path.join(FONT_DIR, pkg, "package", "files", pat)
        _FAM.update(serif=Family(f("source-serif-4", "source-serif-4-latin*-400-normal.woff2")),
                    italic=Family(f("source-serif-4", "source-serif-4-latin*-400-italic.woff2")),
                    sans=Family(f("inter", "inter-latin*-500-normal.woff2")),
                    cjk=Family(f("noto-serif-sc", "noto-serif-sc-*-400-normal.woff2")))
    return _FAM[key]


def _shaped(t, key):
    runs, cur, face0 = [], "", None
    for ch in t:
        face = fam(key).face_for(ch) or fam("cjk").face_for(ch)
        if face is not face0 and cur:
            runs.append((face0, cur))
            cur = ""
        face0, cur = face, cur + ch
    if cur:
        runs.append((face0, cur))
    for face, run in runs:
        buf = hb.Buffer()
        buf.add_str(run)
        buf.guess_segment_properties()
        hb.shape(face.hb, buf, {"kern": True, "liga": True, "lnum": True, "pnum": True})
        yield face, buf


def measure(t, key, size):
    return sum(p.x_advance * size / f.upem for f, b in _shaped(t, key) for p in b.glyph_positions)


def text(t, key, size, x, y, fill, anchor="start", opacity=1):
    x = {"start": x, "middle": x - measure(t, key, size) / 2, "end": x - measure(t, key, size)}[anchor]
    ntos = lambda v: f"{v:.1f}".rstrip("0").rstrip(".")
    d = ""
    for face, buf in _shaped(t, key):
        s = size / face.upem
        for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
            pen = SVGPathPen(face.glyphs, ntos=ntos)
            face.glyphs[face.order[info.codepoint]].draw(
                TransformPen(pen, (s, 0, 0, -s, x + pos.x_offset * s, y - pos.y_offset * s)))
            d += pen.getCommands()
            x += pos.x_advance * s
    op = "" if opacity == 1 else f' fill-opacity="{opacity}"'
    return f'<path d="{d}" fill="{fill}"{op}/>'


# ---------------------------------------------------------------- helpers
def years_made(y):
    return NOW - y if y > 0 else NOW - y - 1


def ago(y):
    return max(NOW - y, 10)


def since(t, zh):
    s = t["since"]
    if zh and "BC" in s:
        s = s.replace("c. ", "约").replace(" BC", "")
        s = ("约" if s.startswith("约") else "") + "前 " + s.lstrip("约")
    return s


def tombs_by_age(data):
    return sorted(data["tombstones"], key=lambda t: t["since_year"])


# ---------------------------------------------------------------- the one image
def timeline(data, lang, theme):
    c = THEMES[theme]
    ink, muted, hair = c["ink"], c["muted"], c["hair"]
    zh = lang == "zh"
    nkey, nsize = ("cjk", 14) if zh else ("serif", 15)
    W, top, row = 960, 64, 26
    L, N = 300, 872
    lo, hi = 1.0, math.log10(50000)
    X = lambda ya: N - (math.log10(ya) - lo) / (hi - lo) * (N - L)
    ts = tombs_by_age(data)
    H = top + row * len(ts) + 92
    out, defs = [], []
    # grid
    for ya, en, cn in ((10000, "10,000 years ago", "一万年前"), (1000, "1,000", "一千年"), (100, "100", "一百年"), (10, "10", "十年")):
        x = X(ya)
        out.append(f'<line x1="{x:.1f}" y1="{top - 14}" x2="{x:.1f}" y2="{H - 70}" stroke="{hair}" stroke-dasharray="2 4"/>')
        out.append(text(cn if zh else en, "cjk" if zh else "serif", 11.5, x, H - 50, muted, "middle"))
    out.append(f'<line x1="{N}" y1="{top - 22}" x2="{N}" y2="{H - 70}" stroke="{ink}" stroke-opacity=".55"/>')
    out.append(text(str(NOW), "serif", 13, N, top - 30, ink, "middle"))
    for i, t in enumerate(ts):
        y = top + i * row
        x0 = X(ago(t["since_year"]))
        out.append(text(t["name_zh"] if zh else t["name"], nkey, nsize, 0, y + 5, ink))
        out.append(text(since(t, zh), "serif", 11.5, x0 - 9, y + 4, muted, "end"))
        out.append(f'<circle cx="{x0:.1f}" cy="{y}" r="2.6" fill="{ink}"/>')
        st = t["status"]
        if st == "declared":
            out.append(f'<line x1="{x0:.1f}" y1="{y}" x2="{N}" y2="{y}" stroke="{ink}" stroke-width="1.25"/>')
            out.append(f'<line x1="{N - 0.5}" y1="{y - 5}" x2="{N - 0.5}" y2="{y + 5}" stroke="{ink}" stroke-width="1.6"/>')
        elif st == "dying":
            gid = f"g{t['id']}"
            defs.append(f'<linearGradient id="{gid}" gradientUnits="userSpaceOnUse" x1="{x0:.1f}" y1="0" x2="{N}" y2="0">'
                        f'<stop offset="0" stop-color="{ink}"/><stop offset=".55" stop-color="{ink}"/>'
                        f'<stop offset="1" stop-color="{ink}" stop-opacity=".06"/></linearGradient>')
            out.append(f'<line x1="{x0:.1f}" y1="{y}" x2="{N}" y2="{y}" stroke="url(#{gid})" stroke-width="1.25"/>')
        else:
            gid = f"g{t['id']}"
            defs.append(f'<linearGradient id="{gid}" gradientUnits="userSpaceOnUse" x1="{N}" y1="0" x2="{W - 4}" y2="0">'
                        f'<stop offset="0" stop-color="{ink}"/><stop offset="1" stop-color="{ink}" stop-opacity="0"/></linearGradient>')
            out.append(f'<line x1="{x0:.1f}" y1="{y}" x2="{N}" y2="{y}" stroke="{ink}" stroke-width="1.25"/>')
            out.append(f'<line x1="{N}" y1="{y}" x2="{W - 4}" y2="{y}" stroke="url(#{gid})" stroke-width="1.25"/>')
    # legend
    ly = H - 16
    lx = 0
    items = (("declared", "declared dead", "已宣告终结"), ("dying", "passing", "正在让位"), ("alive", "still made by hand", "仍由人手完成"))
    for k, en, cn in items:
        lab = cn if zh else en
        key = "cjk" if zh else "italic"
        if k == "declared":
            out.append(f'<line x1="{lx}" y1="{ly - 4}" x2="{lx + 28}" y2="{ly - 4}" stroke="{ink}" stroke-width="1.25"/>'
                       f'<line x1="{lx + 27.5}" y1="{ly - 9}" x2="{lx + 27.5}" y2="{ly + 1}" stroke="{ink}" stroke-width="1.6"/>')
        elif k == "dying":
            defs.append(f'<linearGradient id="lg2" gradientUnits="userSpaceOnUse" x1="{lx}" y1="0" x2="{lx + 28}" y2="0">'
                        f'<stop offset="0" stop-color="{ink}"/><stop offset="1" stop-color="{ink}" stop-opacity=".06"/></linearGradient>')
            out.append(f'<line x1="{lx}" y1="{ly - 4}" x2="{lx + 28}" y2="{ly - 4}" stroke="url(#lg2)" stroke-width="1.25"/>')
        else:
            defs.append(f'<linearGradient id="lg3" gradientUnits="userSpaceOnUse" x1="{lx + 14}" y1="0" x2="{lx + 34}" y2="0">'
                        f'<stop offset="0" stop-color="{ink}"/><stop offset="1" stop-color="{ink}" stop-opacity="0"/></linearGradient>')
            out.append(f'<line x1="{lx}" y1="{ly - 4}" x2="{lx + 34}" y2="{ly - 4}" stroke="url(#lg3)" stroke-width="1.25"/>')
        if k != "declared":
            out.append(f'<line x1="{lx + (28 if k == "dying" else 16)}" y1="{ly - 10}" x2="{lx + (28 if k == "dying" else 16)}" y2="{ly + 2}" stroke="{ink}" stroke-opacity=".55"/>')
        out.append(text(lab, key, 12.5, lx + 42, ly, muted))
        lx += 42 + measure(lab, key, 12.5) + 36
    label = ("Twenty kinds of human work, each drawn from the year it began to the present."
             if not zh else "二十种人类劳动，每一条线从它诞生的那一年画到今天。")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="{label}">'
            f'<title>{label}</title><defs>{"".join(defs)}</defs>{"".join(out)}</svg>\n')


# ---------------------------------------------------------------- markdown
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def fmt_date(d, zh):
    if "-" not in d:
        return d
    y, m = d.split("-")
    return f"{y} 年 {int(m)} 月" if zh else f"{MONTHS[int(m) - 1]} {y}"


ICON_COLOR = "#8b949e"  # mid grey: legible on GitHub's light and dark themes
ICONS = {
    "paper": '<path d="M4 1.75h5.5l2.75 2.75v9.75H4z"/><path d="M9.5 1.75V4.5h2.75M6 7.5h4.25M6 10h4.25"/>',
    "code": '<path d="M5.5 4.5 2 8l3.5 3.5M10.5 4.5 14 8l-3.5 3.5"/>',
    "site": '<circle cx="8" cy="8" r="6.25"/><ellipse cx="8" cy="8" rx="2.6" ry="6.25"/><path d="M1.75 8h12.5"/>',
    "article": '<rect x="2" y="3" width="12" height="10" rx="1.5"/><path d="M4.5 6h7M4.5 8.5h7M4.5 11H9"/>',
    "talk": '<circle cx="8" cy="8" r="6.25"/><path d="M6.75 5.6v4.8L10.6 8z" fill="#8b949e"/>',
    "mourn": '<path d="M8 1.75v12.5M4.5 5.25h7"/>',
    "post": '<path d="M2.75 3.5a1.5 1.5 0 0 1 1.5-1.5h7.5a1.5 1.5 0 0 1 1.5 1.5v6a1.5 1.5 0 0 1-1.5 1.5H7.5L4.5 14v-3h-.25a1.5 1.5 0 0 1-1.5-1.5z"/><path d="M5.5 5.5h5M5.5 8h3"/>',
}
PAPER_HOSTS = {"arxiv.org": "arXiv", "nature.com": "Nature", "proceedings.mlr.press": "PMLR",
               "pubmed.ncbi.nlm.nih.gov": "PubMed", "preprints.org": "Preprints"}
NAMED_HOSTS = {"en.wikipedia.org": "Wikipedia", "spectrum.ieee.org": "IEEE Spectrum", "medium.com": "Medium",
               "marginalrevolution.com": "Marginal Revolution", "pub.towardsai.net": "Towards AI", "anthropic.com": "Anthropic",
               "futurism.com": "Futurism", "technode.com": "TechNode", "thewrap.com": "TheWrap", "billboard.com": "Billboard",
               "ilfoglio.it": "Il Foglio", "theregister.com": "The Register", "theartnewspaper.com": "The Art Newspaper",
               "axios.com": "Axios", "fortune.com": "Fortune", "npr.org": "NPR", "singularityhub.com": "Singularity Hub",
               "thenextweb.com": "TNW", "scientificamerican.com": "Scientific American", "dailyjournal.com": "Daily Journal",
               "nofilk.substack.com": "Substack", "hackernoon.com": "HackerNoon", "tmtpost.com": "钛媒体", "huxiu.com": "虎嗅"}
BLOGS = {"staltz.com", "nolanlawson.com", "cbcrenshaw.com"}


def icon_svg(name):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" width="16" height="16" fill="none" '
            f'stroke="{ICON_COLOR}" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">{ICONS[name]}</svg>\n')


def host(url):
    h = url.split("//", 1)[1].split("/", 1)[0]
    return h[4:] if h.startswith("www.") else h


def classify(url, key=None):
    """Return (icon, label) for a link."""
    h = host(url)
    if h in ("x.com", "twitter.com"):
        handle = url.split(h + "/", 1)[1].split("/", 1)[0]
        return "post", f"X · @{handle}"
    if key:
        k = key.lower()
        icon = {"code": "code", "template": "code", "demo": "site", "site": "site", "report": "paper",
                "analysis": "paper", "paper": "paper", "blog": "article"}.get(k, "article")
        if h == "github.com":
            icon = "code"
        return icon, key
    if h in PAPER_HOSTS:
        return "paper", PAPER_HOSTS[h]
    if h == "github.com":
        return "code", "GitHub"
    if h.endswith("github.io") or h == "claude.ai":
        return "site", "Live demo"
    if "/talks/" in url:
        return "talk", "Talk"
    if h in BLOGS:
        return "article", "Blog"
    if h == "en.wikipedia.org":
        return "article", "Wikipedia"
    path = url.split(h, 1)[1].strip("/")
    if not path:
        return "site", "Website"
    return "article", NAMED_HOSTS.get(h, h)


BTN = {"light": {"bg": "#f6f8fa", "line": "#d0d7de", "ink": "#24292f", "icon": "#57606a"},
       "dark": {"bg": "#21262d", "line": "#363b42", "ink": "#c9d1d9", "icon": "#8b949e"}}
BUTTONS = {}


def btn_slug(icon, label):
    if not label.isascii():
        return f"{icon}-u" + hashlib.md5(label.encode()).hexdigest()[:8]
    a = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    return f"{icon}-{a}"


CAT = {  # one muted hue per kind of source; lighter at night
    "paper":   {"light": "#b0413e", "dark": "#d1574f"},
    "code":    {"light": "#2c7a4b", "dark": "#3fa06a"},
    "site":    {"light": "#2f6fb3", "dark": "#4c8ed9"},
    "article": {"light": "#a86f1a", "dark": "#c99032"},
    "talk":    {"light": "#7a4fb5", "dark": "#9775d1"},
    "mourn":   {"light": "#6e7781", "dark": "#57606a"},
    "post":    {"light": "#0f1419", "dark": "#3d4450"},
}


def button_svg(icon, label, theme):
    c = BTN[theme]
    hue = CAT[icon][theme]
    H, isz, fs = 22, 12, 11.5
    blk = 26
    tw = measure(label, "sans", fs)
    W = round(blk + 8 + tw + 9)
    glyph = ICONS[icon].replace("#8b949e", "#ffffff")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="{label}">'
            f'<clipPath id="r"><rect width="{W}" height="{H}" rx="6"/></clipPath><g clip-path="url(#r)">'
            f'<rect width="{W}" height="{H}" fill="{c["bg"]}"/><rect width="{blk}" height="{H}" fill="{hue}"/></g>'
            f'<rect x=".5" y=".5" width="{W - 1}" height="{H - 1}" rx="5.5" fill="none" stroke="{hue}" stroke-opacity=".55"/>'
            f'<g transform="translate({(blk - isz) / 2} {(H - isz) / 2}) scale({isz / 16})" fill="none" stroke="#ffffff" stroke-width="1.7" '
            f'stroke-linecap="round" stroke-linejoin="round">{glyph}</g>'
            + text(label, "sans", fs, blk + 8, 15.2, c["ink"]) + "</svg>\n")


def button(url, icon, label):
    slug = btn_slug(icon, label)
    BUTTONS[slug] = (icon, label)
    return (f'<a href="{url}"><picture><source media="(prefers-color-scheme: dark)" srcset="docs/assets/buttons/{slug}-dark.svg">'
            f'<img src="docs/assets/buttons/{slug}-light.svg" height="22" alt="{label}"></picture></a>')


def entry(e, zh, ts=None):
    note = (e.get("note_zh") if zh else None) or e.get("note") or ""
    KZ = {"Manifesto": "宣言", "Protocol": "协议", "Demo": "演示", "Counter": "守护", "Critique": "批评", "Commentary": "评论"}
    meta = [("、" if zh else ", ").join(KZ[k] if zh else k.lower() for k in e.get("kinds", []))] if e.get("kinds") else []
    if not (e.get("source") and e["date"] in e["source"]):
        meta.append(fmt_date(e["date"], zh))
    icon, label = classify(e["url"])
    btns = [button(e["url"], icon, label)]
    btns += [button(v, *classify(v, {"site": "Website"}.get(k, k.capitalize()))) for k, v in (e.get("links") or {}).items()]
    if ts and e.get("mourns"):
        t = ts[e["mourns"]]
        btns.append(button(f'#{t["slug"]}', "mourn", t["name_zh"] if zh else t["name"]))
    src = e.get("source")
    by = f" — {src}" if src and src != label and not label.startswith(src) else ""
    s = f"- **[{e['title']}]({e['url']})**{by}"
    if note:
        s += f"<br>\n  {note}"
    return s + f"<br>\n  <sub>{' · '.join(meta)}</sub><br>\n  {' '.join(btns)}\n"


def lower_first(s):
    return s if (len(s) > 1 and s[1].isupper()) else s[:1].lower() + s[1:]


def span(t, zh=False):
    s = since(t, zh)
    return {"declared": f"{s} – {NOW}?", "dying": f"{s} – {NOW}", "alive": f"{s} –"}[t["status"]]


def obituary(t, zh):
    n = years_made(t["since_year"])
    approx = t["since"].startswith("c.")
    st = t["status"]
    by, by_zh = t["by"].replace(" · ", " and "), t["by_zh"].replace(" · ", "、")
    if zh:
        yrs = f"{'约 ' if approx else ''}{n:,} 年"
        life = {"declared": f"人类亲手完成它，已有 {yrs}。{by_zh} 宣告了它的终结，继任者是{t['successor_zh']}。",
                "dying": f"人类亲手完成它，已有 {yrs}；如今正让位于{t['successor_zh']}。宣告者：{by_zh}。",
                "alive": f"人类亲手完成它，已有 {yrs}，至今仍是。挑战来自{t['by_zh']}。"}[st]
        return f"{t['origin_zh']}。{life}"
    yrs = f"{'some ' if approx else ''}{n:,} years"
    life = {"declared": f"Made by hand for {yrs}. Declared dead by {by}; to be succeeded by {lower_first(t['successor'])}.",
            "dying": f"Made by hand for {yrs}, and now passing to {lower_first(t['successor'])}. Declared dead by {by}.",
            "alive": f"Made by hand for {yrs}, and still is. Challenged by {lower_first(t['by'])}."}[st]
    return f"Born {t['origin']}. {life}"


def picture(name, alt, width="100%"):
    return (f'<picture>\n  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/{name}-dark.svg">\n'
            f'  <img src="docs/assets/{name}-light.svg" width="{width}" alt="{html.escape(alt)}">\n</picture>\n')


def mark(theme):
    ink = THEMES[theme]["ink"]
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 132 20" width="132" height="20" role="img" aria-label="">'
            f'<circle cx="4" cy="10" r="2.8" fill="{ink}"/><path d="M4 10H127" stroke="{ink}" stroke-width="1.3"/>'
            f'<path d="M127.2 4v12" stroke="{ink}" stroke-width="1.8"/></svg>\n')


def chapter(p, lang, theme):
    """A quiet chapter heading for each plot: large numeral, title, motto, hairline."""
    c = THEMES[theme]
    zh = lang == "zh"
    W, H = 960, 132
    title = p["name_zh"] if zh else p["name"].replace("Plot of ", "")
    motto = p["motto_zh"] if zh else p["motto"]
    out = [text(p["numeral"], "serif", 72, 0, 92, c["ink"])]
    x = measure(p["numeral"], "serif", 72) + 28
    out.append(f'<line x1="{x - 14:.1f}" y1="36" x2="{x - 14:.1f}" y2="100" stroke="{c["hair"]}" stroke-width="1"/>')
    out.append(text(title, "cjk" if zh else "serif", 34, x, 66, c["ink"]))
    out.append(text(motto, "cjk" if zh else "italic", 15, x, 94, c["muted"]))
    out.append(f'<line x1="0" y1="{H - 1}" x2="{W}" y2="{H - 1}" stroke="{c["hair"]}" stroke-width="1"/>')
    label = html.escape(f'{p["numeral"]} · {title}')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="{label}">'
            f'<title>{label}</title>{"".join(out)}</svg>\n')


def social(data):
    """1280×640 repository social preview (render to PNG and upload in Settings → Social preview)."""
    ink, muted, hair = "#1f2328", "#6e7781", "#d8dee4"
    W, H = 1280, 640
    L, N = 140, 1140
    lo, hi = 1.0, math.log10(50000)
    X = lambda ya: N - (math.log10(ya) - lo) / (hi - lo) * (N - L)
    out = [f'<rect width="{W}" height="{H}" fill="#ffffff"/>']
    ts = tombs_by_age(data)
    for i, t in enumerate(ts):
        y = 360 + i * 11
        x0 = X(ago(t["since_year"]))
        out.append(f'<circle cx="{x0:.1f}" cy="{y}" r="1.8" fill="{muted}"/>'
                   f'<line x1="{x0:.1f}" y1="{y}" x2="{N}" y2="{y}" stroke="{muted}" stroke-width="1"/>')
        if t["status"] != "alive":
            out.append(f'<line x1="{N}" y1="{y - 3.5}" x2="{N}" y2="{y + 3.5}" stroke="{muted}" stroke-width="1.4"/>')
        else:
            out.append(f'<line x1="{N}" y1="{y}" x2="{N + 40}" y2="{y}" stroke="{hair}" stroke-width="1"/>')
    out.append(f'<line x1="{N}" y1="342" x2="{N}" y2="{360 + 11 * len(ts)}" stroke="{ink}" stroke-opacity=".5"/>')
    out.append(text(str(NOW), "serif", 16, N, 332, muted, "middle"))
    out.append(text("Awesome Last Human-Made", "serif", 78, W / 2, 196, ink, "middle"))
    out.append(text("A graveyard of things humans used to make.", "italic", 32, W / 2, 252, muted, "middle"))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}">{"".join(out)}</svg>\n')


def readme(data, lang):
    zh = lang == "zh"
    ts = {t["id"]: t for t in data["tombstones"]}
    o = []
    w = o.append
    w('<div align="center">\n\n<br>\n\n' + picture("mark", "", width="132") + "\n")
    w("# Awesome Last Human-Made\n\n" if not zh else "# 人类手作墓地\n\n")
    w("*A graveyard of things humans used to make.*\n\n" if not zh else "Awesome Last Human-Made · 一座安放人类手作之物的墓地\n\n")
    w("<sub>**English** · [简体中文](README.zh-CN.md)</sub>\n\n" if not zh
      else "<sub>[English](README.md) · **简体中文**</sub>\n\n")
    w("</div>\n\n<br>\n\n")
    if not zh:
        w("Behind every paper, every proof, every song, every verdict, there used to be a pair of hands: "
          "someone who stayed up too late, crossed things out, and at last signed their name.\n\n"
          "Now the works arrive with their epitaphs already carved — *The Last Human-Written Paper*, "
          "*The Last Math Competition*, *The Last AI Built by Humans* — and the machines, quietly, "
          "go on finishing what we began.\n\n"
          "This is where we write it down: when each thing was born, how long our hands held it, "
          "and who said it was over. Most of them, so far, are still breathing.\n\n<br>\n\n")
    else:
        w("每一篇论文、每一个证明、每一首歌、每一份判决背后，曾经都有一双手："
          "有人为它熬过夜，涂改过，最后签下自己的名字。\n\n"
          "如今，作品自己带着墓志铭来了：*The Last Human-Written Paper*、*The Last Math Competition*、"
          "*The Last AI Built by Humans*。而机器正安静地，把我们开了头的事，一件一件做完。\n\n"
          "我们在这里把它们记下来：每一样东西生于何时，在人的手里留了多少年，又是谁说它已经结束。"
          "到目前为止，它们大多还在呼吸。\n\n<br>\n\n")
    sfx = "-zh" if zh else ""
    w(picture(f"timeline{sfx}", "Timeline of twenty forms of human work" if not zh else "二十种人类劳动的时间线"))
    w('\n<p align="center"><sub>' + ("" if zh else "<i>")
      + ("Each line runs from the year a form of work began to the present. Twenty of them, some forty thousand years long, meet in the same year."
         if not zh else "每条线从一种劳动诞生的那一年画到今天。二十条线，最长的约有四万年，在同一年相遇。")
      + ("" if zh else "</i>") + "</sub></p>\n\n<br>\n\n")
    # index
    w("| " + ("Form | Life | Now" if not zh else "形式 | 生卒 | 现状") + "\n| :-- | :-- | :-- |\n")
    stat = {"declared": ("declared dead", "已宣告终结"), "dying": ("passing", "正在让位"), "alive": ("still made by hand", "仍由人手完成")}
    for p in data["plots"]:
        for sid in p["stones"]:
            t = ts[sid]
            w(f"| [{t['name_zh'] if zh else t['name']}](#{t['slug']}) | {span(t, zh)} | <sub>{stat[t['status']][1] if zh else "<i>" + stat[t['status']][0] + "</i>"}</sub> |\n")
    w("\n<br>\n\n")
    divider = ('<p align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="docs/assets/mark-dark.svg">'
               '<img src="docs/assets/mark-light.svg" width="80" alt=""></picture></p>\n\n')
    for p in data["plots"]:
        title = p["name_zh"] if zh else p["name"].replace("Plot of ", "")
        motto = p["motto_zh"] if zh else f'<i>{p["motto"]}</i>'
        sfx = "-zh" if zh else ""
        w(f'<br>\n\n<a id="plot-{p["id"]}"></a>\n\n' + picture(f"chapters/{p['id']}{sfx}", f"{p['numeral']} · {title}") + "\n<br>\n\n")
        for i, sid in enumerate(p["stones"]):
            t = ts[sid]
            if i:
                w("<br>\n\n" + divider + "<br>\n\n")
            w(f'<a id="{t["slug"]}"></a>\n\n### {t["name_zh"] if zh else t["name"]}\n\n')
            w(f"**{span(t, zh)}**\n\n{obituary(t, zh)}\n\n")
            w(f"> {t['epitaph_zh']}\n\n" if zh else f"> *{t['epitaph']}*\n\n")
            for e in t["entries"]:
                w(entry(e, zh))
        w("\n<br>\n\n")
    w("<br>\n\n## " + ("Elegies" if not zh else "挽歌") + "\n\n")
    w(("Written by people mourning the work they used to do.\n\n" if not zh else "那些为自己曾经亲手做的事写下悼文的人。\n\n"))
    for e in data["elegies"]:
        w(entry(e, zh, ts))
    w("\n<br>\n\n## " + ("Ancestors" if not zh else "先祖") + "\n\n")
    w(("The pattern is older than it looks.\n\n" if not zh else "这种句式比看上去更古老。\n\n"))
    for e in data["ancestors"]:
        w(entry(e, zh))
    if zh:
        w("\n## 中文语境\n\n")
        for e in data["press_zh"]:
            w(entry(dict(e, date="2026"), zh))
    if not zh:
        w("\n## Scope\n\n"
          "A work is listed if it declares the end of a human-made form because AI can now make it (*manifesto*); "
          "proposes an agent-native replacement (*protocol*); shows AI making a whole instance of the form, with a public record (*demo*); "
          "or answers one of these (*counter*, *critique*, *commentary*).\n\n"
          "Not listed: works that merely contain the word “last”, AI tools that do not claim to replace a form, and claims we could not trace "
          "to a primary or reputable source. Contested cases wait until the dust settles.\n\n"
          "Birth years mark a conventional beginning, not an invention. Statuses are editorial and move with the evidence, in either direction.\n\n")
        w("## Contributing\n\n"
          "Everything on this page is generated from [`data/graveyard.yml`](data/graveyard.yml). "
          "Add an entry there, run `pip install -r scripts/requirements.txt && python scripts/build.py` "
          "(Python 3.9+ and Node.js; fonts are fetched once), and open a pull request. "
          "If you are not sure where it belongs, an issue with a title, a link and one sentence is enough.\n\n")
        w("## Citation\n\n```bibtex\n@misc{awesome_last_human_made,\n"
          "  title        = {Awesome Last Human-Made: A Graveyard of Things Humans Used to Make},\n"
          "  author       = {Zhao, Haoyu},\n  year         = {2026},\n"
          f"  howpublished = {{\\url{{https://github.com/{REPO}}}}}\n}}\n```\n\n")
    else:
        w("\n## 收录范围\n\n"
          "收录以下作品：因为 AI 已经能做，而宣告某种人类产出形式终结的（宣言）；提出面向智能体的替代形式的（协议）；"
          "展示 AI 完整做出这种形式、并有公开记录的（演示）；以及回应以上任何一类的（守护、批评、评论）。\n\n"
          "不收录：只是名字里带 “last” 的作品、没有宣称取代某种形式的 AI 工具，以及无法追溯到一手或可靠来源的说法。有争议的案例，等尘埃落定再说。\n\n"
          "诞生年份取的是约定俗成的起点，而非发明时间。状态是编者的判断，会随证据向任一方向变化。\n\n")
        w("## 参与贡献\n\n"
          "本页全部内容由 [`data/graveyard.yml`](data/graveyard.yml) 生成。在其中添加条目，运行 `pip install -r scripts/requirements.txt && python scripts/build.py`"
          "（需要 Python 3.9+ 和 Node.js，字体只在第一次运行时下载），然后提交 PR。"
          "不确定放在哪里？提一个 issue，写下标题、链接和一句话就够了。\n\n")
    if not zh:
        w("## License\n\nThe list is dedicated to the public domain under [CC0 1.0](LICENSE). "
          "The build script is released under the [MIT License](scripts/LICENSE). "
          "Linked works belong to their authors.\n\n")
    else:
        w("## 许可\n\n清单内容以 [CC0 1.0](LICENSE) 贡献至公有领域；构建脚本采用 [MIT 许可证](scripts/LICENSE)。"
          "所链接的作品归其作者所有。\n\n")
    w("<br>\n\n<p align=\"center\"><sub>" + ("" if zh else "<i>")
      + ("Everything here was made by someone who thought it would last." if not zh else "这里的每一样东西，都曾被某个人以为会长久存在。")
      + ("" if zh else "</i>") + "</sub></p>\n")
    return "".join(o)


def write(rel, content):
    path = os.path.join(ROOT, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def main():
    ensure_fonts()
    with open(os.path.join(ROOT, "data", "graveyard.yml"), encoding="utf-8") as f:
        data = yaml.safe_load(f)
    for lang, sfx in (("en", ""), ("zh", "-zh")):
        for theme in THEMES:
            write(f"docs/assets/timeline{sfx}-{theme}.svg", timeline(data, lang, theme))
    for theme in THEMES:
        write(f"docs/assets/mark-{theme}.svg", mark(theme))
        for lang, sfx in (("en", ""), ("zh", "-zh")):
            for p in data["plots"]:
                write(f"docs/assets/chapters/{p['id']}{sfx}-{theme}.svg", chapter(p, lang, theme))
    write("README.md", readme(data, "en"))
    write("README.zh-CN.md", readme(data, "zh"))
    for slug, (icon, label) in sorted(BUTTONS.items()):
        for theme in BTN:
            write(f"docs/assets/buttons/{slug}-{theme}.svg", button_svg(icon, label, theme))
    write("docs/assets/social-preview.svg", social(data))
    print("built", len(data["tombstones"]), "obituaries")


if __name__ == "__main__":
    main()
