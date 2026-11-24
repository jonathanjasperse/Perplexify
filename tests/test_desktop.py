"""The native window host and how the app chooses between it and the browser. A fake pywebview stands in for the
real one, so these tests need no screen, no WebView2 and no pythonnet."""
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from helpers import TempHome

from perplexify import app as shell
from perplexify import desktop, paths


class FakeWindow:
    def __init__(self, title, url, **options):
        self.title, self.url, self.options = title, url, options
        self.events = types.SimpleNamespace(closing=FakeEvent(), shown=FakeEvent(), before_load=FakeEvent())
        self.width, self.height, self.x, self.y = 1100, 700, 40, 50
        self.native = types.SimpleNamespace(WindowState="Normal", Handle=types.SimpleNamespace(ToInt64=lambda: 0))
        self.destroyed = False
        self.asked = []
        self.answer = True
        self.dialogs = []
        self.dialog_result = None

    def destroy(self):
        self.destroyed = True

    def create_confirmation_dialog(self, title, message):
        self.asked.append((title, message))
        return self.answer

    def create_file_dialog(self, which, **kw):
        self.dialogs.append((which, kw))
        if isinstance(self.dialog_result, Exception):
            raise self.dialog_result
        return self.dialog_result

    def restore(self):
        pass

    def show(self):
        pass


class FakeEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, fn):
        self.handlers.append(fn)
        return self


def fake_webview(start_raises=None, show_window=True):
    """A stand-in for the pywebview module that records what the app asks of it."""
    mod = types.ModuleType("webview")
    mod.created, mod.started = [], []

    class FileDialog:
        OPEN, FOLDER, SAVE = 10, 20, 30

    mod.FileDialog = FileDialog
    mod.settings = {"OPEN_DEVTOOLS_IN_DEBUG": True}

    def create_window(title, url, **options):
        w = FakeWindow(title, url, **options)
        mod.created.append(w)
        return w

    def start(**options):
        mod.started.append(options)
        w = mod.created[-1]
        if show_window:
            for fn in w.events.shown.handlers:
                fn()
        if start_raises:
            raise start_raises

    mod.create_window, mod.start = create_window, start
    return mod


class Pure(TempHome):
    def setUp(self):
        (paths.home() / "window.json").unlink(missing_ok=True)

    def test_remembered_state_round_trips_and_ignores_nonsense(self):
        self.assertEqual(desktop.load_state(), {})
        desktop.save_state(width=1400, height=900, x=20, y=30, maximized=False, theme="light")
        self.assertEqual(desktop.load_state(), {"width": 1400, "height": 900, "x": 20, "y": 30, "maximized": False, "theme": "light"})
        (paths.home() / "window.json").write_text(json.dumps({"width": 5, "height": "tall", "x": 1e9, "theme": "neon", "maximized": "yes"}), "utf-8")
        self.assertEqual(desktop.load_state(), {})   # too small, not a number, off the map, unknown, not a boolean
        (paths.home() / "window.json").write_text("{broken", "utf-8")
        self.assertEqual(desktop.load_state(), {})

    def test_saving_one_thing_keeps_the_rest(self):
        desktop.save_state(width=1500, height=950)
        desktop.save_state(theme="light")
        self.assertEqual(desktop.load_state(), {"width": 1500, "height": 950, "theme": "light"})

    def test_a_position_is_only_reused_when_it_has_both_coordinates(self):
        self.assertFalse(desktop.fits_on_screen({"width": 1200}))
        self.assertFalse(desktop.fits_on_screen({"x": 10}))

    def test_a_new_window_is_cut_down_to_the_screen_and_centred(self):
        with mock.patch.object(desktop, "work_area", return_value=(0, 0, 1440, 852)):     # a laptop screen without its taskbar
            self.assertEqual(desktop.window_geometry({}), (1280, 820, (80, 16)))           # 1280 x 860 would hang under the taskbar

    def test_a_roomy_screen_gets_the_usual_size(self):
        with mock.patch.object(desktop, "work_area", return_value=(0, 0, 2560, 1400)):
            self.assertEqual(desktop.window_geometry({}), (1280, 860, (640, 270)))

    def test_without_a_screen_size_the_window_system_places_it(self):
        with mock.patch.object(desktop, "work_area", return_value=None):
            self.assertEqual(desktop.window_geometry({}), (1280, 860, None))

    def test_a_remembered_window_returns_where_it_was_but_never_bigger_than_the_main_screen(self):
        state = {"width": 1100, "height": 750, "x": 150, "y": 100}
        with mock.patch.object(desktop, "work_area", return_value=(0, 0, 1440, 852)), mock.patch.object(desktop, "fits_on_screen", return_value=True):
            self.assertEqual(desktop.window_geometry(state), (1100, 750, (150, 100)))
            self.assertEqual(desktop.window_geometry(dict(state, width=2000, height=1200, y=0)), (1440, 852, (0, 0)))
            self.assertEqual(desktop.window_geometry(dict(state, y=500)), (1100, 750, (150, 102)))     # pulled up: its bottom edge on screen
            self.assertEqual(desktop.window_geometry(dict(state, x=1600, width=1800, height=1000)), (1800, 1000, (1600, 100)))   # another monitor

    def test_a_position_that_is_gone_is_replaced_by_the_centre(self):
        with mock.patch.object(desktop, "work_area", return_value=(0, 0, 1440, 852)), mock.patch.object(desktop, "fits_on_screen", return_value=False):
            self.assertEqual(desktop.window_geometry({"width": 1100, "height": 750, "x": 9000, "y": 9000}), (1100, 750, (170, 51)))

    def test_title_bar_colours_are_windows_colorrefs(self):
        self.assertEqual(desktop._colorref("#112233"), 0x332211)   # 0x00BBGGRR
        self.assertEqual(desktop._colorref("#16171b"), 0x1b1716)

    def test_both_themes_have_the_colours_the_window_needs(self):
        for theme in ("dark", "light"):
            self.assertEqual(set(desktop.THEMES[theme]), {"bg", "fg", "border"})

    def test_the_window_can_be_switched_off(self):
        with mock.patch.dict(os.environ, {"PERPLEXIFY_NO_WINDOW": "1"}):
            ok, why = desktop.support()
        self.assertFalse(ok)
        self.assertIn("PERPLEXIFY_NO_WINDOW", why)

    def test_without_pywebview_the_answer_is_no_with_a_reason(self):
        with mock.patch.dict(sys.modules, {"webview": None}):
            ok, why = desktop.support()
        self.assertFalse(ok)
        self.assertIn("pywebview", why)

    def test_the_webview2_version_is_none_off_windows(self):
        with mock.patch.object(desktop, "IS_WINDOWS", False):
            self.assertIsNone(desktop.webview2_runtime_version())


class Window(TempHome):
    def setUp(self):
        (paths.home() / "window.json").unlink(missing_ok=True)

    def make(self, busy=lambda: False, **kw):
        fake = fake_webview(**kw)
        win = desktop.NativeWindow("http://127.0.0.1:9999", busy=busy)
        patcher = mock.patch.dict(sys.modules, {"webview": fake})
        patcher.start()
        self.addCleanup(patcher.stop)
        return win, fake

    def test_it_opens_the_page_in_a_window_that_remembers_things(self):
        win, fake = self.make()
        self.assertTrue(win.run())
        created = fake.created[0]
        self.assertEqual((created.title, created.url), ("Perplexify", "http://127.0.0.1:9999"))
        self.assertTrue(created.options["text_select"])
        self.assertEqual(created.options["min_size"], desktop.MIN_SIZE)
        self.assertEqual(created.options["background_color"], desktop.THEMES["dark"]["bg"])
        started = fake.started[0]
        self.assertIs(started["private_mode"], False)                     # localStorage (theme, ticks) must survive restarts
        self.assertTrue(Path(started["storage_path"]).is_dir())
        self.assertTrue(str(Path(started["storage_path"])).startswith(str(paths.home())))
        self.assertIn("icon", started)
        self.assertEqual(win.mode, "window")
        self.assertIs(started["debug"], False)                            # no inspector, no browser shortcuts, in everyday use
        self.assertTrue(fake.settings["OPEN_DEVTOOLS_IN_DEBUG"])          # untouched

    def test_debug_mode_keeps_the_developer_tools_closed_until_asked_for(self):
        fake = fake_webview()
        win = desktop.NativeWindow("http://127.0.0.1:9999", debug=True)
        with mock.patch.dict(sys.modules, {"webview": fake}):
            win.run()
        self.assertIs(fake.started[0]["debug"], True)                     # F12, reload and "Inspect" work
        self.assertFalse(fake.settings["OPEN_DEVTOOLS_IN_DEBUG"])         # but no inspector window pops up on every start

    def test_the_window_opens_at_a_size_that_fits_the_screen(self):
        win, fake = self.make()
        with mock.patch.object(desktop, "work_area", return_value=(0, 0, 1440, 852)):
            win.run()
        o = fake.created[0].options
        self.assertEqual((o["width"], o["height"], o["x"], o["y"]), (1280, 820, 80, 16))

    def test_it_never_falls_back_to_the_old_internet_explorer_engine(self):
        win, fake = self.make()
        with mock.patch.object(desktop, "IS_WINDOWS", True), mock.patch.object(desktop, "set_app_user_model_id"), \
                mock.patch.object(desktop, "style_titlebar"):
            win.run()
        self.assertEqual(fake.started[0]["gui"], "edgechromium")

    def test_it_reports_failure_when_the_window_never_appeared(self):
        win, fake = self.make(start_raises=RuntimeError("WebView2 failed"), show_window=False)
        self.assertFalse(win.run())
        self.assertIn("WebView2 failed", win.problem)

    def test_a_failure_after_the_window_was_shown_is_not_reported_as_unusable(self):
        win, fake = self.make(start_raises=RuntimeError("late"), show_window=True)
        self.assertTrue(win.run())   # it ran: the caller must not also open a browser

    def test_without_pywebview_it_reports_failure(self):
        win = desktop.NativeWindow("http://127.0.0.1:1")
        with mock.patch.dict(sys.modules, {"webview": None}):
            self.assertFalse(win.run())
        self.assertIn("pywebview", win.problem)

    def test_closing_an_idle_window_just_remembers_its_size(self):
        win, fake = self.make()
        win.run()
        w = fake.created[0]
        self.assertTrue(w.events.closing.handlers[0]())
        self.assertEqual(w.asked, [])
        self.assertEqual({k: desktop.load_state()[k] for k in ("width", "height", "x", "y")}, {"width": 1100, "height": 700, "x": 40, "y": 50})

    def test_a_maximized_window_remembers_only_that(self):
        win, fake = self.make()
        win.run()
        fake.created[0].native.WindowState = "Maximized"
        self.assertTrue(fake.created[0].events.closing.handlers[0]())
        self.assertIs(desktop.load_state()["maximized"], True)
        self.assertNotIn("width", desktop.load_state())

    def test_closing_while_indexing_asks_first(self):
        win, fake = self.make(busy=lambda: True)
        win.run()
        w = fake.created[0]
        w.answer = False
        self.assertIs(w.events.closing.handlers[0](), False)       # cancelled: the window stays
        self.assertEqual(len(w.asked), 1)
        self.assertIn("indexing", w.asked[0][1])
        w.answer = True
        self.assertTrue(w.events.closing.handlers[0]())             # confirmed

    def test_quitting_the_app_never_asks(self):
        win, fake = self.make(busy=lambda: True)
        win.run()
        win.close()
        self.assertTrue(fake.created[0].destroyed)
        self.assertTrue(fake.created[0].events.closing.handlers[0]())
        self.assertEqual(fake.created[0].asked, [])

    def test_a_busy_check_that_fails_does_not_trap_the_user_in_the_window(self):
        def broken():
            raise RuntimeError("boom")

        win, fake = self.make(busy=broken)
        win.run()
        self.assertTrue(fake.created[0].events.closing.handlers[0]())

    def test_the_title_bar_follows_the_theme_and_the_choice_is_remembered(self):
        win, fake = self.make()
        win.run()
        with mock.patch.object(desktop, "style_titlebar") as style:
            win.set_theme("light")
            win.set_theme("neon")                  # ignored
        style.assert_called_once()
        self.assertEqual(style.call_args.args[1], "light")
        self.assertEqual(desktop.load_state()["theme"], "light")
        self.assertEqual(desktop.NativeWindow("http://x").theme, "light")   # the next start begins in the same theme

    def test_the_folder_and_file_dialogs_are_the_native_ones(self):
        win, fake = self.make()
        win.run()
        w = fake.created[0]
        w.dialog_result = ("C:\\Users\\me\\manuals",)
        self.assertEqual(win.pick_folder(), "C:\\Users\\me\\manuals")
        self.assertEqual(w.dialogs[0][0], 20)                                         # FileDialog.FOLDER
        w.dialog_result = ("a.pdf", "b.md")
        self.assertEqual(win.pick_files(), ["a.pdf", "b.md"])
        self.assertEqual(w.dialogs[1][0], 10)                                         # FileDialog.OPEN
        self.assertTrue(w.dialogs[1][1]["allow_multiple"])
        self.assertTrue(any("*.pdf" in t for t in w.dialogs[1][1]["file_types"]))
        w.dialog_result = None                                                         # cancelled
        self.assertIsNone(win.pick_folder())
        self.assertEqual(win.pick_files(), [])
        w.dialog_result = RuntimeError("dialog failed")
        self.assertIsNone(win.pick_folder())

    def test_dialogs_before_the_window_exists_do_nothing(self):
        win = desktop.NativeWindow("http://x")
        self.assertIsNone(win.pick_folder())
        self.assertEqual(win.pick_files(), [])
        win.show()   # nothing to show: must not raise
        win.close()


class Calls(unittest.TestCase):
    """What the page asks of the shell through the server."""

    class Host:
        mode = "window"

        def __init__(self):
            self.log = []

        def show(self):
            self.log.append("show")

        def set_theme(self, theme):
            self.log.append(("theme", theme))

        def pick_folder(self, title=""):
            return "C:\\picked"

        def pick_files(self, title=""):
            return ["C:\\a.pdf"]

    def client(self, host):
        from perplexify.context import AppContext
        from perplexify.server import create_app

        return TestClient(create_app(AppContext(), shutdown=lambda: None, host=host), base_url="http://127.0.0.1")

    @classmethod
    def setUpClass(cls):
        TempHome.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        TempHome.tearDownClass.__func__(cls)

    def test_the_page_is_told_where_it_is_shown(self):
        host = self.Host()
        self.assertEqual(self.client(host).get("/api/bootstrap").json()["shell"], "window")
        self.assertEqual(self.client(None).get("/api/bootstrap").json()["shell"], "server")

    def test_a_second_launch_can_bring_the_window_to_the_front(self):
        host = self.Host()
        self.assertEqual(self.client(host).post("/api/window/show").status_code, 200)
        self.assertEqual(host.log, ["show"])
        self.assertEqual(self.client(None).post("/api/window/show").status_code, 400)

    def test_the_theme_reaches_the_window_and_only_real_themes(self):
        host, client = self.Host(), None
        client = self.client(host)
        self.assertEqual(client.post("/api/window/theme", json={"theme": "light"}).status_code, 200)
        self.assertEqual(client.post("/api/window/theme", json={"theme": "neon"}).status_code, 200)
        self.assertEqual(host.log, [("theme", "light")])
        self.assertEqual(self.client(None).post("/api/window/theme", json={"theme": "dark"}).status_code, 200)  # harmless without a window

    def test_the_pickers_use_the_window_when_there_is_one(self):
        client = self.client(self.Host())
        self.assertEqual(client.post("/api/pick-folder").json(), {"path": "C:\\picked"})
        self.assertEqual(client.post("/api/pick-files").json(), {"paths": ["C:\\a.pdf"]})

    def test_without_a_window_the_pickers_fall_back_to_the_system_dialogs(self):
        with mock.patch("perplexify.server.osutil.pick_folder", return_value="/x"), \
                mock.patch("perplexify.server.osutil.pick_files", return_value=["/y"]):
            client = self.client(None)
            self.assertEqual(client.post("/api/pick-folder").json(), {"path": "/x"})
            self.assertEqual(client.post("/api/pick-files").json(), {"paths": ["/y"]})


class Starting(TempHome):
    """run_app: the window, and the browser when there is no window."""

    def run_app(self, **kw):
        with mock.patch.object(shell, "EXIT_WATCHDOG", 0), mock.patch.object(shell.osutil, "acquire_single_instance", return_value=True), \
                mock.patch.object(shell, "setup_logging"):
            return shell.run_app(port=shell._free_port(19765), **kw)

    def test_the_window_is_used_when_it_is_possible(self):
        seen = {}

        class FakeNative:
            mode = "window"

            def __init__(self, url, busy, debug):
                seen.update(url=url, debug=debug)
                self.problem = ""

            def run(self):
                info = json.loads(paths.run_info_path().read_text("utf-8"))   # the server is already up and registered
                seen["run_info"] = info
                seen["health"] = shell._probe(info["port"])
                return True

            def close(self):
                seen["closed"] = True

        opened = []
        with mock.patch.object(shell.desktop, "support", return_value=(True, "fake")), \
                mock.patch.object(shell.desktop, "NativeWindow", FakeNative), mock.patch.object(shell.webbrowser, "open", lambda u: opened.append(u)):
            self.assertEqual(self.run_app(debug=True), 0)
        self.assertEqual(seen["run_info"]["mode"], "window")
        self.assertEqual(desktop.load_state().get("port"), seen["run_info"]["port"])    # remembered for the next start
        self.assertTrue(seen["health"])
        self.assertTrue(seen["debug"])
        self.assertTrue(seen["closed"])           # the app quits with its window
        self.assertEqual(opened, [])              # no browser anywhere
        self.assertFalse(paths.run_info_path(create=False).exists())

    def test_the_browser_is_used_when_the_window_is_not_possible(self):
        opened = []
        with mock.patch.object(shell.desktop, "support", return_value=(False, "no WebView2")), \
                mock.patch("perplexify.tray.run_tray", return_value=True), mock.patch.object(shell.webbrowser, "open", lambda u: opened.append(u)):
            self.assertEqual(self.run_app(), 0)
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0].startswith("http://127.0.0.1:"))

    def test_a_window_that_cannot_be_opened_falls_back_to_the_browser(self):
        class Broken:
            mode = "window"
            problem = "WebView2 would not start"

            def __init__(self, url, busy, debug):
                pass

            def run(self):
                return False

            def close(self):
                pass

        opened = []
        with mock.patch.object(shell.desktop, "support", return_value=(True, "fake")), mock.patch.object(shell.desktop, "NativeWindow", Broken), \
                mock.patch("perplexify.tray.run_tray", return_value=True), mock.patch.object(shell.webbrowser, "open", lambda u: opened.append(u)):
            self.assertEqual(self.run_app(), 0)
        self.assertEqual(len(opened), 1)

    def test_browser_mode_can_be_asked_for_and_can_stay_silent(self):
        opened = []
        with mock.patch("perplexify.tray.run_tray", return_value=True), mock.patch.object(shell.webbrowser, "open", lambda u: opened.append(u)), \
                mock.patch.object(shell.desktop, "support") as support:
            self.assertEqual(self.run_app(window=False, open_browser=False), 0)
        support.assert_not_called()                # --browser never even asks about the window
        self.assertEqual(opened, [])

    def test_a_second_launch_brings_the_first_window_forward(self):
        posted = []
        with mock.patch.object(shell, "running_instance", return_value=4321), \
                mock.patch.object(shell.httpx, "post", lambda url, **kw: posted.append(url) or types.SimpleNamespace(status_code=200)), \
                mock.patch.object(shell, "setup_logging"):
            self.assertEqual(shell.run_app(), 0)
        self.assertEqual(posted, ["http://127.0.0.1:4321/api/window/show"])

    def test_a_second_launch_opens_the_browser_when_the_first_copy_cannot_show_a_window(self):
        opened = []
        with mock.patch.object(shell, "running_instance", return_value=4321), mock.patch.object(shell.httpx, "post", side_effect=shell.httpx.ConnectError("down")), \
                mock.patch.object(shell.webbrowser, "open", lambda u: opened.append(u)), mock.patch.object(shell, "setup_logging"):
            self.assertEqual(shell.run_app(), 0)
        self.assertEqual(opened, ["http://127.0.0.1:4321"])


class BrowserFallback(unittest.TestCase):
    def test_the_browser_host_opens_the_page_and_uses_system_dialogs(self):
        host = shell.BrowserHost("http://127.0.0.1:1")
        with mock.patch.object(shell.webbrowser, "open") as op:
            host.show()
        op.assert_called_once_with("http://127.0.0.1:1")
        host.set_theme("dark")   # nothing to restyle
        with mock.patch.object(shell.osutil, "pick_folder", return_value="/f"), mock.patch.object(shell.osutil, "pick_files", return_value=["/g"]):
            self.assertEqual(host.pick_folder(), "/f")
            self.assertEqual(host.pick_files(), ["/g"])

    def test_the_switch_can_be_repointed_at_the_browser(self):
        class A:
            mode = "window"

        switch = shell.HostSwitch(A())
        self.assertEqual(switch.mode, "window")
        switch.target = shell.BrowserHost("http://x")
        self.assertEqual(switch.mode, "browser")
        self.assertTrue(callable(switch.pick_folder))


class Ports(TempHome):
    def setUp(self):
        (paths.home() / "window.json").unlink(missing_ok=True)

    def test_the_port_is_the_one_used_last_time_unless_another_was_asked_for(self):
        self.assertEqual(shell._choose_port(None, 8765, None), 8765)
        self.assertEqual(shell._choose_port(None, None, None), 8765)
        self.assertEqual(shell._choose_port(None, 8765, 8767), 8767)    # 8765 was busy last time: stay put, the page keeps its settings
        self.assertEqual(shell._choose_port(None, 9000, 8767), 9000)    # a port chosen in Settings wins over the remembered one
        self.assertEqual(shell._choose_port(9100, 9000, 8767), 9100)    # --port wins over everything

    def test_the_port_is_remembered_with_the_window_state_and_silly_ones_are_ignored(self):
        desktop.save_state(port=8767, theme="light")
        self.assertEqual(desktop.load_state(), {"port": 8767, "theme": "light"})
        desktop.save_state(port=80)
        self.assertNotIn("port", desktop.load_state())


class SelfTestWindowCheck(unittest.TestCase):
    """The self-test proves the window's libraries load without opening a window."""

    def fakes(self, renderer="edgechromium"):
        guilib = types.ModuleType("webview.guilib")
        guilib.initialize = mock.Mock(return_value=types.SimpleNamespace(renderer=renderer))
        web = fake_webview()
        web.guilib = guilib
        return guilib, {"webview": web, "webview.guilib": guilib, "clr": types.ModuleType("clr")}

    def run_check(self, modules, runtime, windows=True):
        from perplexify import selftest

        with mock.patch.object(desktop, "IS_WINDOWS", windows), mock.patch.object(desktop, "IS_MAC", False), \
                mock.patch.object(desktop, "webview2_runtime_version", return_value=runtime), \
                mock.patch.dict(sys.modules, modules), mock.patch("importlib.metadata.version", return_value="6.2.1"):
            return selftest.window_support()

    def test_a_platform_without_a_native_window_is_fine(self):
        self.assertIn("browser", self.run_check({}, None, windows=False))

    def test_a_pc_without_the_runtime_is_a_note_not_a_failure_and_loads_nothing_more(self):
        guilib, modules = self.fakes()
        self.assertIn("no WebView2 Runtime", self.run_check(modules, None))
        guilib.initialize.assert_not_called()       # on such a PC pywebview would switch to the old Internet Explorer engine

    def test_with_the_runtime_the_window_classes_are_loaded(self):
        guilib, modules = self.fakes()
        self.assertIn("WebView2 Runtime 157.0.1", self.run_check(modules, "157.0.1"))
        guilib.initialize.assert_called_once_with("edgechromium")

    def test_the_old_engine_is_a_failure(self):
        _, modules = self.fakes(renderer="mshtml")
        with self.assertRaises(AssertionError):
            self.run_check(modules, "157.0.1")

    def test_a_missing_package_is_a_failure(self):
        with self.assertRaises(ImportError):
            self.run_check({"webview": None}, "157.0.1")


if __name__ == "__main__":
    unittest.main()
