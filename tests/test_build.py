"""Builds the example site, then breaks it on purpose to prove each guard fires."""

import json
import pathlib
import re
import shutil
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import geo_build  # noqa: E402


def quiet(_msg):
    pass


class ExampleSite(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="geo-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.site = self.tmp / "site"
        shutil.copytree(ROOT / "example", self.site)
        self.out = self.tmp / "out"

    def build(self, **kw):
        return geo_build.build(self.site, self.out, log=quiet, **kw)

    def edit(self, rel, old, new):
        f = self.site / rel
        text = f.read_text(encoding="utf-8")
        self.assertIn(old, text)
        f.write_text(text.replace(old, new, 1), encoding="utf-8")

    def assertFails(self, pattern):
        with self.assertRaisesRegex(geo_build.BuildError, pattern):
            self.build()

    # --- happy path ------------------------------------------------------------------------

    def test_example_builds(self):
        res = self.build()
        self.assertEqual(res["pages"], 6)
        self.assertEqual(res["warnings"], [])
        for rel in ("index.html", "fr/index.html", "about/index.html", "404.html",
                    "sitemap.xml", "llms.txt", "robots.txt", "assets/site.css"):
            self.assertTrue((self.out / rel).is_file(), rel)

    def test_jsonld_and_hreflang(self):
        self.build()
        page = (self.out / "about" / "index.html").read_text(encoding="utf-8")
        ld = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', page, re.S).group(1))
        types = [n["@type"] for n in ld["@graph"]]
        for t in ("Organization", "WebSite", "Person", "WebPage", "BreadcrumbList", "FAQPage"):
            self.assertIn(t, types)
        faq = next(n for n in ld["@graph"] if n["@type"] == "FAQPage")
        self.assertEqual(len(faq["mainEntity"]), 3)
        self.assertIn('hreflang="fr" href="https://www.example.com/fr/a-propos/"', page)
        self.assertIn('hreflang="x-default" href="https://www.example.com/about/"', page)
        self.assertEqual(page.count("<h1"), 1)

    def test_sitemap_and_llms(self):
        self.build()
        sitemap = (self.out / "sitemap.xml").read_text(encoding="utf-8")
        self.assertEqual(sitemap.count("<url>"), 6)
        self.assertIn('hreflang="en" href="https://www.example.com/"', sitemap)
        llms = (self.out / "llms.txt").read_text(encoding="utf-8")
        self.assertTrue(llms.startswith("# Juniper & Rye"))
        self.assertIn("](https://www.example.com/fr/guides/levain-naturel/)", llms)

    def test_base_url_override(self):
        self.build(base_url="https://staging.example.org/")
        self.assertIn("https://staging.example.org/sitemap.xml", (self.out / "robots.txt").read_text())

    def test_jsonld_cannot_close_script_tag(self):
        self.edit("content/en/about.html", "<summary>How many people join a bread class?</summary>",
                  "<summary>Is &lt;/script&gt; escaped?</summary>")
        self.build()
        page = (self.out / "about" / "index.html").read_text(encoding="utf-8")
        ld = re.search(r'<script type="application/ld\+json">(.*?)</script>', page, re.S).group(1)
        self.assertIn("<\\/script>", ld)

    # --- guards -----------------------------------------------------------------------------

    def test_guard_em_dash(self):
        self.edit("content/en/home.html", "slowly</h1>", "slowly — really</h1>")
        self.assertFails("em dash")

    def test_guard_verify_marker(self):
        self.edit("content/en/home.html", "for 18 to 24 hours before", "for [VERIFY] hours before")
        self.assertFails(r"\[VERIFY\]")

    def test_guard_banned_phrase(self):
        self.edit("content/en/home.html", "<h2>Learn", "<p>Coming soon</p><h2>Learn")
        self.assertFails("banned phrase")

    def test_guard_single_h1(self):
        self.edit("content/en/home.html", "<h2>Learn", "<h1>Learn")
        self.assertFails("exactly one <h1>")

    def test_guard_broken_internal_link_and_draft(self):
        self.edit("content/en/home.html", 'href="/about/"', 'href="/contact/"')
        self.assertFails("unbuilt path /contact/")
        res = self.build(draft=True)
        self.assertTrue(any("/contact/" in w for w in res["warnings"]))

    def test_guard_hreflang_reciprocal(self):
        self.edit("content/fr/a-propos.html", '"alt": {"en": "/about/"},', "")
        self.assertFails("does not link back")

    def test_guard_meta_required(self):
        self.edit("content/en/about.html", '"title": "About Juniper & Rye | Bakery, team and classes",', "")
        self.assertFails("non-empty 'title'")

    def test_guard_duplicate_path(self):
        self.edit("content/en/about.html", '"path": "/about/"', '"path": "/"')
        self.assertFails("already used")

    def test_cli_exit_code(self):
        self.assertEqual(geo_build.main([str(self.site), "-o", str(self.out)]), 0)
        self.edit("content/en/home.html", "<h2>Learn", "<h1>Learn")
        self.assertEqual(geo_build.main([str(self.site), "-o", str(self.out)]), 1)


if __name__ == "__main__":
    unittest.main()
