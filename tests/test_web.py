"""Things about the web page that must not change by accident."""
import re
import unittest
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "perplexify" / "web" / "index.html"


class Defaults(unittest.TestCase):
    def setUp(self):
        self.html = PAGE.read_text("utf-8")

    def test_dark_mode_is_the_default_whatever_the_system_setting(self):
        self.assertRegex(self.html, r'<html[^>]*data-theme="dark"')            # dark before any script runs
        self.assertNotIn("prefers-color-scheme", self.html)                     # the system setting is not consulted
        script = re.search(r"<script>(.*?)</script>", self.html, re.S).group(1)
        self.assertRegex(script, r"if \(t !== 'light' && t !== 'dark'\) t = 'dark';")  # nothing chosen: dark

    def test_a_remembered_choice_still_wins(self):
        script = re.search(r"<script>(.*?)</script>", self.html, re.S).group(1)
        self.assertIn("localStorage.getItem('perplexify.theme')", script)


class Wiring(unittest.TestCase):
    """The page is plain files: a script or stylesheet that is not linked, or a view that has no place, would only show up as a blank screen."""

    def setUp(self):
        self.html = PAGE.read_text("utf-8")
        self.web = PAGE.parent

    def test_every_linked_file_exists_and_every_script_is_linked(self):
        linked = re.findall(r'<script src="([^"]+)"', self.html) + re.findall(r'<link rel="stylesheet" href="([^"]+)"', self.html)
        for name in linked:
            self.assertTrue((self.web / name).is_file(), name)
        on_disk = {f"js/{p.name}" for p in (self.web / "js").glob("*.js")}
        self.assertEqual(on_disk - set(linked), set())

    def test_main_starts_last_so_everything_it_uses_is_defined(self):
        scripts = re.findall(r'<script src="([^"]+)"', self.html)
        self.assertEqual(scripts[0], "js/core.js")
        self.assertEqual(scripts[-1], "js/main.js")

    def test_the_three_views_have_their_places(self):
        for view in ("transcript", "composer", "searchview", "historyview", "viewer", "viewtabs"):
            self.assertIn(f'id="{view}"', self.html)


CSS = PAGE.with_name("style.css").read_text("utf-8")


def theme_tokens(theme: str) -> dict[str, str]:
    block = re.search(r':root\[data-theme="%s"\]\s*\{(.*?)\n\}' % theme, CSS, re.S).group(1)
    return dict(re.findall(r"--([a-z-]+):\s*([^;]+);", block))


class Styling(unittest.TestCase):
    def test_both_themes_define_the_same_colour_tokens(self):
        self.assertEqual(set(theme_tokens("dark")), set(theme_tokens("light")))

    def test_every_variable_the_stylesheet_uses_is_defined(self):
        defined = set(theme_tokens("dark")) | set(re.findall(r"--([a-z-]+):", re.search(r":root \{(.*?)\n\}", CSS, re.S).group(1)))
        used = set(re.findall(r"var\(--([a-z-]+)", CSS))
        self.assertEqual(used - defined, set())

    def test_the_window_title_bar_has_the_colours_of_the_page(self):
        from perplexify import desktop

        for theme in ("dark", "light"):
            t = theme_tokens(theme)
            self.assertEqual(desktop.THEMES[theme], {"bg": t["bg"], "fg": t["text"], "border": t["line"]}, theme)

    def test_no_stray_colours_outside_the_stylesheet(self):
        # colours live in the tokens, so a theme change reaches everything (the tab icon in index.html is the one exception)
        web = PAGE.parent
        for path in (web / "js").glob("*.js"):
            self.assertIsNone(re.search(r"#[0-9a-fA-F]{6}\b|rgba?\(", path.read_text("utf-8")), path.name)

    def test_text_colours_stay_readable_on_every_surface(self):
        def lum(rgb):
            def ch(c):
                c /= 255
                return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
            r, g, b = (ch(c) for c in rgb)
            return 0.2126 * r + 0.7152 * g + 0.0722 * b

        def rgb(h):
            return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))

        for theme in ("dark", "light"):
            t = theme_tokens(theme)
            for text in ("text", "muted", "faint", "accent", "ok", "warn", "err"):
                for surface in ("bg", "panel", "raise", "btn"):
                    a, b = lum(rgb(t[text])), lum(rgb(t[surface]))
                    ratio = (max(a, b) + 0.05) / (min(a, b) + 0.05)
                    self.assertGreaterEqual(ratio, 4.5, f"{theme}: {text} on {surface} is {ratio:.2f}:1")


if __name__ == "__main__":
    unittest.main()
