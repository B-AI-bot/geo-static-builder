# geo-static-builder

A single-file static site builder that produces pages search engines and AI answer engines can read without guessing: one JSON-LD graph per page, reciprocal `hreflang` links, a multilingual sitemap and an `llms.txt`. It also refuses to build when the content has mistakes you do not want published.

Python standard library only. No framework, no `npm install`, no runtime.

## Why

Generative engine optimisation (GEO) is mostly unglamorous plumbing. Assistants and crawlers quote pages they can parse with confidence: who publishes the site, who wrote the page, which language version is which, what the questions and answers are. Most site builders leave that to plugins, and the plumbing silently breaks when someone edits a page.

This builder makes the plumbing part of the build, and turns the common mistakes into build failures instead of things you find months later in Search Console:

| Guard | Fails the build when |
|---|---|
| Headings | a page does not have exactly one `<h1>` |
| Metadata | a page has no `path`, `title` or `description`, or two pages share a path |
| Internal links | a link points to a path that is not built and is not a static file (`--draft` turns this into a warning) |
| Translations | page A declares page B as its translation but B does not point back to A (`hreflang` must be reciprocal) |
| Fact-check markers | an unresolved `[VERIFY]` marker is left in the text |
| Typography | an em dash appears in published text (can be switched off) |
| Banned phrases | a phrase from your `banned_phrases` list appears (regular expressions, case-insensitive) |

Title and description lengths outside your target range produce warnings, not failures.

## What it generates

For every page in `content/<lang>/*.html`:

- `<title>`, meta description, canonical URL, robots meta
- `<link rel="alternate" hreflang>` for each translation, plus `x-default`
- Open Graph and Twitter card tags
- one `<script type="application/ld+json">` holding a `@graph` with `Organization`, `WebSite`, `Person` (author), `WebPage` or `Article`, `BreadcrumbList` (when the page declares crumbs), `FAQPage` (built automatically from `<details class="faq">` blocks) and any extra schema.org nodes you add in the page meta
- cache-busted stylesheet and script links (`?v=<content hash>`)

And for the site: `sitemap.xml` with `xhtml:link` language alternates, `llms.txt` (see [llmstxt.org](https://llmstxt.org)), `robots.txt` (unless you ship your own in `static/`) and `404.html`.

## Quick start

Requires Python 3.9 or later.

```bash
git clone <this repo> && cd geo-static-builder
python3 geo_build.py example -o dist
# built 6 pages -> .../dist
python3 -m http.server 8000 --directory dist
```

`example/` is a small bilingual (English and French) site for **Juniper & Rye**, a fictional bakery. Copy it to start your own site.

## Site layout

```
my-site/
├── site.json            site settings
├── layout.html          page template
├── content/
│   ├── en/home.html     one file per page, any file name
│   └── fr/accueil.html
└── static/              copied as is (CSS, images, favicon, robots.txt...)
```

### A page

Each content file starts with a JSON header in an HTML comment, followed by the inner HTML of `<main>`:

```html
<!--meta {
  "path": "/guides/sourdough-starter/",
  "alt": {"fr": "/fr/guides/levain-naturel/"},
  "type": "article",
  "title": "How to make a sourdough starter in 7 days",
  "description": "A day-by-day method to make a sourdough starter from flour and water...",
  "published": "2026-09-15",
  "modified": "2026-10-01",
  "crumbs": [["Sourdough starter guide", "/guides/sourdough-starter/"]]
}-->
<h1>How to make a sourdough starter in 7 days</h1>
<p>...</p>
<details class="faq"><summary>Why is my starter not rising?</summary><p>It is usually too cold.</p></details>
```

| Key | Required | Meaning |
|---|---|---|
| `path` | yes | URL path, starting and ending with `/`. The page is written to `<path>/index.html`. |
| `title`, `description` | yes | `<title>` and meta description, also used in JSON-LD, Open Graph and `llms.txt` |
| `alt` | no | translations, as `{"<lang>": "<path>"}`. Must be reciprocal. |
| `type` | no | `article` gives `Article` + `og:type=article`; anything else gives `WebPage` |
| `headline`, `og_title`, `og_image` | no | overrides for the matching fields |
| `published`, `modified` | no | ISO dates; `modified` is also the sitemap `lastmod` |
| `crumbs` | no | breadcrumb trail after the home page, as `[["Name", "/path/"], ...]` |
| `noindex` | no | `true` keeps the page out of the sitemap and `llms.txt` and sets `noindex` |
| `schema` | no | extra schema.org nodes appended to the graph. A `Service` gets `provider` set to your organization. |

The language comes from the folder name (`content/fr/...`), which must be declared in `site.json`.

### site.json

See [`example/site.json`](example/site.json) for a complete file. The main keys:

| Key | Meaning |
|---|---|
| `name`, `base_url`, `description` | site name, absolute base URL, one-sentence summary |
| `default_lang` | language used for `x-default` and the 404 page |
| `organization` | `name`, `email`, `logo`, `same_as` (list of profile URLs) for the `Organization` node |
| `author` | `name`, `url`, optional `id` and `same_as` for the `Person` node |
| `languages` | per language: `locale` (`en-US`), `home`, `nav` (`[["Label", "/path/"]]`), `label` for the language switcher, `breadcrumb_home`, `footer`, `llms_heading`, optional `og_image` |
| `stylesheets`, `scripts` | files under `static/` linked on every page with a content hash |
| `og_image` | default social image |
| `llms` | `summary` and `facts` (lines copied into `llms.txt`) |
| `guards` | `no_em_dash` (default `true`), `banned_phrases`, `title_length`, `description_length` |
| `not_found` | `title`, `description`, `body` of the 404 page |

### layout.html

A plain HTML file with `$placeholders` (Python `string.Template`): `$lang`, `$head`, `$site_name`, `$home`, `$nav`, `$lang_switch`, `$main`, `$footer`, `$scripts`, `$year`. Write a literal dollar sign as `$$`.

## Configuration

Command-line flags, with environment variable fallbacks documented in [`.env.example`](.env.example):

```
python3 geo_build.py [SITE_DIR] [-o OUT_DIR] [--base-url URL] [--draft]
```

| Flag | Variable | Default |
|---|---|---|
| `SITE_DIR` | `GEO_SITE_DIR` | `example` |
| `-o, --out` | `GEO_OUT_DIR` | `dist` (deleted and rebuilt on every run) |
| `--base-url` | `GEO_BASE_URL` | `base_url` from `site.json` |
| `--draft` | `GEO_DRAFT=1` | off |

Exit code is `0` on success and `1` on a failed guard, with the reason on stderr:

```
BUILD FAILED: content/en/home.html: needs exactly one <h1>, found 2
```

## Tests

```bash
make test        # or: python3 -m unittest discover -s tests -v
```

The suite builds the example site, checks the JSON-LD, `hreflang`, sitemap and `llms.txt`, then breaks a copy of the site once per guard to prove each one fires.

## Limits

- Content is hand-written HTML fragments, not Markdown. Use your own converter upstream if you want Markdown.
- No incremental builds, no dev server with live reload, no image processing: every run rebuilds the whole site.
- `hreflang` values use the language part of `locale` (`en`, `fr`); regional variants (`en-GB` vs `en-US`) are not distinguished.
- The internal link check looks at `href="/..."` attributes only. Links built by JavaScript, relative links (`href="page/"`) and external links are not checked.
- FAQ extraction only recognises `<details class="faq"><summary>...</summary>...</details>`.
- Structured data helps machines read a page; it does not guarantee rich results or citations by any search or AI engine.

## License

MIT, see [LICENSE](LICENSE).

## About

Built by Loic Guyon at AI Jungle, https://www.welcometotheaijungle.com
