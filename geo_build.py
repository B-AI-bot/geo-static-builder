#!/usr/bin/env python3
"""geo-static-builder: a small static site builder for search and AI answer engines.

Reads a site directory:

    site.json                 site settings (name, base URL, languages, guards, ...)
    layout.html               page template ($placeholders, see README)
    content/<lang>/*.html     one file per page: a <!--meta {json}--> header + the inner HTML of <main>
    static/                   copied as is into the output

and writes a ready-to-serve folder with, for every page, canonical and hreflang
links, Open Graph tags and one JSON-LD @graph (Organization, WebSite, Person,
WebPage or Article, BreadcrumbList, FAQPage); plus sitemap.xml with language
alternates, llms.txt, robots.txt and 404.html.

The build fails (exit 1) on content that should never ship: a page without
exactly one <h1>, a missing title or description, an internal link to a page
that is not built, a translation link that is not reciprocal, an unresolved
[VERIFY] marker, an em dash, or a banned phrase.

Standard library only. Python 3.9+.
"""
import argparse
import datetime
import hashlib
import html
import json
import os
import pathlib
import re
import shutil
import string
import sys

EM_DASH = "\u2014"
GENERATED = {"/sitemap.xml", "/llms.txt", "/robots.txt", "/404.html"}  # written after the page checks


class BuildError(Exception):
    pass


# --- loading -----------------------------------------------------------------------------------

def load_site(site_dir, base_url=None):
    cfg = json.loads((site_dir / "site.json").read_text(encoding="utf-8"))
    if base_url:
        cfg["base_url"] = base_url
    cfg["base_url"] = cfg["base_url"].rstrip("/")
    for key in ("name", "base_url", "default_lang", "languages"):
        if not cfg.get(key):
            raise BuildError(f"site.json: missing {key!r}")
    if cfg["default_lang"] not in cfg["languages"]:
        raise BuildError("site.json: default_lang must be one of languages")
    cfg.setdefault("guards", {})
    return cfg


def load_pages(site_dir, cfg):
    pages, seen = [], {}
    for f in sorted((site_dir / "content").glob("*/*.html")):
        lang = f.parent.name
        rel = str(f.relative_to(site_dir))
        if lang not in cfg["languages"]:
            raise BuildError(f"{rel}: language folder {lang!r} is not declared in site.json")
        raw = f.read_text(encoding="utf-8")
        m = re.match(r"<!--meta\s*(\{.*?\})\s*-->\s*", raw, re.S)
        if not m:
            raise BuildError(f"{rel}: missing <!--meta {{...}}--> header")
        try:
            meta = json.loads(m.group(1))
        except json.JSONDecodeError as e:
            raise BuildError(f"{rel}: invalid meta JSON ({e})") from None
        meta.update(lang=lang, body=raw[m.end():], src=rel)
        for key in ("path", "title", "description"):
            if not str(meta.get(key, "")).strip():
                raise BuildError(f"{rel}: meta needs a non-empty {key!r}")
        if not (meta["path"].startswith("/") and meta["path"].endswith("/")):
            raise BuildError(f"{rel}: path must start and end with '/' ({meta['path']})")
        if meta["path"] in seen:
            raise BuildError(f"{rel}: path {meta['path']} already used by {seen[meta['path']]}")
        seen[meta["path"]] = rel
        pages.append(meta)
    if not pages:
        raise BuildError("no page found under content/<lang>/*.html")
    return pages


# --- helpers -----------------------------------------------------------------------------------

def text_of(fragment):
    """Visible text of an HTML fragment (scripts and tags removed)."""
    fragment = re.sub(r"<(script|style)\b.*?</\1>", " ", fragment, flags=re.S | re.I)
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment))).strip()


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()[:10]


def faq_items(body):
    """Question/answer pairs from <details class="faq"><summary>Q</summary>A</details> blocks."""
    items = []
    for q, a in re.findall(r'<details\s+class="faq"[^>]*>\s*<summary>(.*?)</summary>(.*?)</details>', body, re.S):
        items.append({"@type": "Question", "name": text_of(q),
                      "acceptedAnswer": {"@type": "Answer", "text": text_of(a)}})
    return items


def hreflang(cfg, lang):
    return cfg["languages"][lang]["locale"].split("-")[0]


def alternates(page, by_path, cfg):
    """{lang: path} for a page and its translations (empty when it has none)."""
    alts = page.get("alt") or {}
    if not alts:
        return {}
    out = {page["lang"]: page["path"]}
    out.update({by_path[p]["lang"]: p for p in alts.values() if p in by_path})
    return out


# --- JSON-LD -----------------------------------------------------------------------------------

def jsonld(page, cfg, url):
    base, lang = cfg["base_url"], cfg["languages"][page["lang"]]
    org_id, site_id = base + "/#organization", base + "/#website"
    org = cfg.get("organization", {})
    graph = [{
        "@type": "Organization", "@id": org_id, "name": org.get("name", cfg["name"]), "url": base + "/",
        **({"logo": base + org["logo"] if org["logo"].startswith("/") else org["logo"]} if org.get("logo") else {}),
        **({"email": org["email"]} if org.get("email") else {}),
        **({"sameAs": org["same_as"]} if org.get("same_as") else {}),
        **({"description": cfg["description"]} if cfg.get("description") else {}),
    }, {
        "@type": "WebSite", "@id": site_id, "url": base + "/", "name": cfg["name"], "publisher": {"@id": org_id},
        "inLanguage": [lg["locale"] for lg in cfg["languages"].values()],
    }]
    author = cfg.get("author")
    person_id = None
    if author:
        person_id = author.get("id") or (author.get("url", base + "/").split("#")[0] + "#person")
        graph.append({"@type": "Person", "@id": person_id, "name": author["name"],
                      **({"url": author["url"]} if author.get("url") else {}),
                      **({"sameAs": author["same_as"]} if author.get("same_as") else {})})
        graph[0]["founder"] = {"@id": person_id}
    is_article = page.get("type") == "article"
    webpage = {
        "@type": "Article" if is_article else "WebPage", "@id": url + "#page", "url": url,
        "name": page["title"], "headline": page.get("headline", page["title"])[:110],
        "description": page["description"], "inLanguage": lang["locale"],
        "isPartOf": {"@id": site_id}, "publisher": {"@id": org_id},
    }
    if person_id:
        webpage["author"] = {"@id": person_id}
    for key, field in (("published", "datePublished"), ("modified", "dateModified")):
        if page.get(key):
            webpage[field] = page[key]
    if is_article:
        webpage["mainEntityOfPage"] = url
    crumbs = page.get("crumbs")
    if crumbs:
        items = [{"@type": "ListItem", "position": 1, "name": lang.get("breadcrumb_home", "Home"), "item": base + lang["home"]}]
        items += [{"@type": "ListItem", "position": i, "name": n, "item": base + p} for i, (n, p) in enumerate(crumbs, 2)]
        graph.append({"@type": "BreadcrumbList", "@id": url + "#breadcrumb", "itemListElement": items})
        webpage["breadcrumb"] = {"@id": url + "#breadcrumb"}
    graph.append(webpage)
    faqs = faq_items(page["body"])
    if faqs:
        graph.append({"@type": "FAQPage", "@id": url + "#faq", "mainEntity": faqs, "inLanguage": lang["locale"]})
    for extra in page.get("schema", []):
        extra = dict(extra)
        if extra.get("@type") == "Service":
            extra.setdefault("provider", {"@id": org_id})
        graph.append(extra)
    data = json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False, separators=(",", ":"))
    return data.replace("</", "<\\/")  # never let page text close the <script> tag


# --- rendering ---------------------------------------------------------------------------------

def render(page, cfg, by_path, layout, asset_versions):
    base, lang = cfg["base_url"], cfg["languages"][page["lang"]]
    url = base + page["path"]
    esc = html.escape
    alts = alternates(page, by_path, cfg)
    head = [
        f"<title>{esc(page['title'])}</title>",
        f'<meta name="description" content="{esc(page["description"])}">',
        f'<link rel="canonical" href="{url}">',
        '<meta name="robots" content="' + ("noindex, follow" if page.get("noindex") else "index, follow, max-image-preview:large") + '">',
    ]
    for lg, path in sorted(alts.items()):
        head.append(f'<link rel="alternate" hreflang="{hreflang(cfg, lg)}" href="{base + path}">')
    if cfg["default_lang"] in alts:
        head.append(f'<link rel="alternate" hreflang="x-default" href="{base + alts[cfg["default_lang"]]}">')
    og_image = page.get("og_image") or lang.get("og_image") or cfg.get("og_image")
    head += [
        f'<meta property="og:type" content="{"article" if page.get("type") == "article" else "website"}">',
        f'<meta property="og:site_name" content="{esc(cfg["name"])}">',
        f'<meta property="og:locale" content="{lang["locale"].replace("-", "_")}">',
        f'<meta property="og:title" content="{esc(page.get("og_title", page["title"]))}">',
        f'<meta property="og:description" content="{esc(page["description"])}">',
        f'<meta property="og:url" content="{url}">',
    ]
    if og_image:
        head.append(f'<meta property="og:image" content="{base + og_image if og_image.startswith("/") else og_image}">')
        head.append('<meta name="twitter:card" content="summary_large_image">')
    for css in cfg.get("stylesheets", []):
        head.append(f'<link rel="stylesheet" href="/{css}?v={asset_versions[css]}">')
    head.append(f'<script type="application/ld+json">{jsonld(page, cfg, url)}</script>')

    nav = "".join(
        f'<a href="{esc(h)}"' + (' aria-current="page"' if h == page["path"] else "") + f">{esc(n)}</a>"
        for n, h in lang.get("nav", []))
    switch = "".join(
        f'<a class="lang" href="{path}" hreflang="{hreflang(cfg, lg)}" lang="{hreflang(cfg, lg)}">{esc(cfg["languages"][lg].get("label", lg.upper()))}</a>'
        for lg, path in sorted(alts.items()) if lg != page["lang"])
    scripts = "".join(f'<script defer src="/{js}?v={asset_versions[js]}"></script>' for js in cfg.get("scripts", []))
    return string.Template(layout).safe_substitute(
        lang=hreflang(cfg, page["lang"]), head="\n".join(head), site_name=esc(cfg["name"]),
        home=lang["home"], nav=nav, lang_switch=switch, main=page["body"], scripts=scripts,
        footer=lang.get("footer", ""), year=str(datetime.date.today().year))


# --- guards ------------------------------------------------------------------------------------

def check(page, out_html, built_paths, out_dir, cfg, by_path, draft, warn):
    src, guards = page["src"], cfg["guards"]
    visible = " ".join((text_of(page["body"]), page["title"], page["description"]))
    if guards.get("no_em_dash", True) and EM_DASH in visible:
        raise BuildError(f"{src}: em dash in published text")
    if "[VERIFY" in visible:
        raise BuildError(f"{src}: unresolved [VERIFY] marker")
    banned = guards.get("banned_phrases") or []
    if banned:
        hit = re.search("|".join(banned), visible, re.I)
        if hit:
            raise BuildError(f'{src}: banned phrase "{hit.group(0)}"')
    h1 = len(re.findall(r"<h1[\s>]", out_html, re.I))
    if h1 != 1:
        raise BuildError(f"{src}: needs exactly one <h1>, found {h1}")
    if "{{" in out_html:
        raise BuildError(f"{src}: unreplaced {{{{placeholder}}}}")
    lo, hi = guards.get("title_length", (20, 70))
    if not lo <= len(page["title"]) <= hi:
        warn(f"{page['path']}: title length {len(page['title'])} (aim for {lo}-{hi})")
    lo, hi = guards.get("description_length", (110, 165))
    if not lo <= len(page["description"]) <= hi:
        warn(f"{page['path']}: description length {len(page['description'])} (aim for {lo}-{hi})")
    for lg, target in (page.get("alt") or {}).items():
        other = by_path.get(target)
        if other is None:
            raise BuildError(f"{src}: translation {lg} -> {target} is not a built page")
        if (other.get("alt") or {}).get(page["lang"]) != page["path"]:
            raise BuildError(f"{src}: {target} does not link back to {page['path']} in its 'alt' (hreflang must be reciprocal)")
    for href in re.findall(r'href="(/[^"#?]*)', out_html):
        if href in built_paths or href in GENERATED or (out_dir / href.lstrip("/")).is_file():
            continue
        if draft:
            warn(f"draft: {src} links to unbuilt {href}")
        else:
            raise BuildError(f"{src}: internal link to unbuilt path {href}")


# --- site files --------------------------------------------------------------------------------

def sitemap(pages, by_path, cfg):
    base, urls = cfg["base_url"], []
    for p in pages:
        if p.get("noindex"):
            continue
        alts = alternates(p, by_path, cfg)
        links = "".join(f'<xhtml:link rel="alternate" hreflang="{hreflang(cfg, lg)}" href="{base + path}"/>'
                        for lg, path in sorted(alts.items()))
        if cfg["default_lang"] in alts:
            links += f'<xhtml:link rel="alternate" hreflang="x-default" href="{base + alts[cfg["default_lang"]]}"/>'
        lastmod = f"<lastmod>{p['modified']}</lastmod>" if p.get("modified") else ""
        urls.append(f"<url><loc>{base + p['path']}</loc>{lastmod}{links}</url>")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:xhtml="http://www.w3.org/1999/xhtml">\n'
            + "\n".join(urls) + "\n</urlset>\n")


def llms_txt(pages, cfg):
    """llms.txt (https://llmstxt.org): what the site is, key facts, and every indexable page."""
    llms = cfg.get("llms", {})
    lines = [f"# {cfg['name']}", ""]
    summary = llms.get("summary") or cfg.get("description")
    if summary:
        lines += [f"> {summary}", ""]
    lines += llms.get("facts", [])
    if llms.get("facts"):
        lines.append("")
    for lg, conf in cfg["languages"].items():
        lines.append(f"## {conf.get('llms_heading', 'Pages (' + lg + ')')}")
        lines += [f"- [{p['title']}]({cfg['base_url'] + p['path']}): {p['description']}"
                  for p in pages if p["lang"] == lg and not p.get("noindex")]
        lines.append("")
    return "\n".join(lines)


def build(site_dir, out_dir, base_url=None, draft=False, log=print):
    site_dir, out_dir = pathlib.Path(site_dir).resolve(), pathlib.Path(out_dir).resolve()
    cfg = load_site(site_dir, base_url)
    pages = load_pages(site_dir, cfg)
    by_path = {p["path"]: p for p in pages}
    layout = (site_dir / "layout.html").read_text(encoding="utf-8")
    static = site_dir / "static"
    asset_versions = {}
    for rel in cfg.get("stylesheets", []) + cfg.get("scripts", []):
        f = static / rel
        if not f.is_file():
            raise BuildError(f"site.json: asset {rel} not found in static/")
        asset_versions[rel] = file_hash(f)

    warnings = []

    def warn(msg):
        warnings.append(msg)
        log("warn " + msg)

    if out_dir.exists():
        shutil.rmtree(out_dir)
    if static.is_dir():
        shutil.copytree(static, out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    built_paths = set(by_path)
    for p in pages:
        out_html = render(p, cfg, by_path, layout, asset_versions)
        check(p, out_html, built_paths, out_dir, cfg, by_path, draft, warn)
        dest = out_dir / p["path"].lstrip("/") / "index.html"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(out_html, encoding="utf-8")

    nf_cfg = cfg.get("not_found", {})
    lang = cfg["languages"][cfg["default_lang"]]
    nf = {"lang": cfg["default_lang"], "path": "/404/", "noindex": True, "src": "404",
          "title": nf_cfg.get("title", f"Page not found | {cfg['name']}"),
          "description": nf_cfg.get("description", "This page does not exist."),
          "body": nf_cfg.get("body", f'<h1>Page not found</h1><p><a href="{lang["home"]}">Back to the home page</a></p>')}
    (out_dir / "404.html").write_text(render(nf, cfg, by_path, layout, asset_versions), encoding="utf-8")

    (out_dir / "sitemap.xml").write_text(sitemap(pages, by_path, cfg), encoding="utf-8")
    (out_dir / "llms.txt").write_text(llms_txt(pages, cfg), encoding="utf-8")
    if not (out_dir / "robots.txt").exists():
        (out_dir / "robots.txt").write_text(f"User-agent: *\nAllow: /\n\nSitemap: {cfg['base_url']}/sitemap.xml\n", encoding="utf-8")
    log(f"built {len(pages)} pages -> {out_dir}" + (f" ({len(warnings)} warnings)" if warnings else ""))
    return {"pages": len(pages), "warnings": warnings}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("site", nargs="?", default=os.environ.get("GEO_SITE_DIR", "example"),
                    help="site directory (default: $GEO_SITE_DIR or ./example)")
    ap.add_argument("-o", "--out", default=os.environ.get("GEO_OUT_DIR", "dist"),
                    help="output directory, wiped on each build (default: $GEO_OUT_DIR or ./dist)")
    ap.add_argument("--base-url", default=os.environ.get("GEO_BASE_URL"),
                    help="override site.json base_url (default: $GEO_BASE_URL)")
    ap.add_argument("--draft", action="store_true", default=os.environ.get("GEO_DRAFT") == "1",
                    help="links to unbuilt pages become warnings (default: $GEO_DRAFT=1)")
    args = ap.parse_args(argv)
    try:
        build(args.site, args.out, base_url=args.base_url, draft=args.draft)
    except BuildError as e:
        print(f"BUILD FAILED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
