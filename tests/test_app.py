"""The app shell: quitting a running copy (`perplexify stop`, used by the installer) and crash reporting."""
import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import TempHome

from perplexify import app as shell, osutil, paths


class StopCommand(TempHome):
    def test_nothing_running_is_fine(self):
        self.assertEqual(shell.stop_running(timeout=1), 0)

    def test_looking_for_a_running_copy_never_creates_the_data_folder(self):
        # the uninstaller runs "stop" on PCs where the app may never have been used: that must not leave an empty folder behind
        fresh = Path(self._home) / "never-created"
        old = os.environ["PERPLEXIFY_HOME"]
        os.environ["PERPLEXIFY_HOME"] = str(fresh)
        try:
            self.assertEqual(shell.stop_running(timeout=1), 0)
            self.assertIsNone(shell.running_instance())
        finally:
            os.environ["PERPLEXIFY_HOME"] = old
        self.assertFalse(fresh.exists())

    def test_a_leftover_run_file_is_ignored(self):
        paths.run_info_path().write_text(json.dumps({"pid": 0, "port": 9, "version": "x"}), "utf-8")
        self.assertEqual(shell.stop_running(timeout=1), 0)

    def test_it_stops_a_running_server_and_returns_once_it_is_gone(self):
        import uvicorn

        from perplexify.context import AppContext
        from perplexify.server import create_app

        holder = {}
        app = create_app(AppContext(), shutdown=lambda: setattr(holder["server"], "should_exit", True))
        port = shell._free_port(18765)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False))
        holder["server"] = server
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.time() + 15
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(server.started)
        paths.run_info_path().write_text(json.dumps({"pid": 0, "port": port, "version": "x"}), "utf-8")
        self.assertTrue(shell._probe(port))
        self.assertEqual(shell.stop_running(timeout=15), 0)
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertFalse(shell._probe(port))


class ProcessChecks(unittest.TestCase):
    def test_each_data_folder_has_its_own_single_instance_lock(self):
        old = os.environ.pop("PERPLEXIFY_HOME", None)
        try:
            self.assertEqual(osutil.mutex_name(), osutil.MUTEX_NAME)  # the installed app, default folder
            os.environ["PERPLEXIFY_HOME"] = r"C:\one"
            a = osutil.mutex_name()
            os.environ["PERPLEXIFY_HOME"] = r"C:\TWO"
            b = osutil.mutex_name()
            os.environ["PERPLEXIFY_HOME"] = r"c:\ONE"
            self.assertNotEqual(a, b)
            self.assertNotEqual(a, osutil.MUTEX_NAME)
            self.assertEqual(osutil.mutex_name(), a)  # paths compare without regard to case
        finally:
            if old is None:
                os.environ.pop("PERPLEXIFY_HOME", None)
            else:
                os.environ["PERPLEXIFY_HOME"] = old

    def test_pid_alive(self):
        self.assertTrue(osutil.pid_alive(os.getpid()))
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        self.assertFalse(osutil.pid_alive(child.pid))


class CrashReport(TempHome):
    def test_unexpected_errors_are_logged_and_printed_and_give_exit_code_1(self):
        from perplexify import __main__ as entry

        with mock.patch("perplexify.cli.main", side_effect=RuntimeError("boom")), mock.patch.object(sys, "stderr", io.StringIO()) as err:
            self.assertEqual(entry._main(), 1)
        self.assertIn("RuntimeError: boom", (paths.logs_dir() / "crash.log").read_text("utf-8"))
        self.assertIn("boom", err.getvalue())

    def test_exit_codes_pass_through(self):
        from perplexify import __main__ as entry

        with mock.patch("perplexify.cli.main", side_effect=SystemExit(3)):
            with self.assertRaises(SystemExit) as cm:
                entry._main()
        self.assertEqual(cm.exception.code, 3)


if __name__ == "__main__":
    unittest.main()
