import asyncio
import os
import signal
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

import browser_background
from linux_background import LinuxBackgroundDisplay, _LinuxProcess
from sungnam_client import BrowserLaunchError, SungnamClient, _wait_for_browser_port


class PlatformTests(unittest.TestCase):
    def test_linux_factory_does_not_import_windows_module(self):
        display = Mock()
        with patch("browser_background.sys.platform", "linux"), \
             patch.dict(sys.modules, {"windows_background": None}), \
             patch("linux_background.LinuxBackgroundDisplay", return_value=display):
            self.assertIs(browser_background.create_background_desktop(), display)

    def test_profiles_are_separate_and_windows_path_is_preserved(self):
        for platform, name in (("linux", ".nodriver_profile_linux"), ("win32", ".nodriver_profile")):
            with self.subTest(platform=platform), patch("browser_background.sys.platform", platform):
                self.assertEqual(browser_background.default_profile_directory("project"),
                                 os.path.join("project", name))

    def test_missing_xvfb_tools_have_install_instructions(self):
        with patch("linux_background.shutil.which", side_effect=lambda name: None if name == "xauth" else name):
            with self.assertRaisesRegex(OSError, "xauth.*apt-get install xvfb xauth"):
                LinuxBackgroundDisplay()


class LinuxLaunchTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_port_waits_for_slow_startup(self):
        writer = Mock(wait_closed=AsyncMock())
        process = Mock(returncode=None)
        with patch("sungnam_client.asyncio.open_connection", new_callable=AsyncMock,
                   side_effect=[ConnectionRefusedError(), (Mock(), writer)]) as connect, \
             patch("sungnam_client.asyncio.sleep", new_callable=AsyncMock):
            await _wait_for_browser_port("127.0.0.1", 1234, process)
        self.assertEqual(connect.await_count, 2)
        writer.close.assert_called_once()
        writer.wait_closed.assert_awaited_once()

    async def test_browser_exit_during_startup_is_reported(self):
        with self.assertRaisesRegex(BrowserLaunchError, "종료 코드: 3"):
            await _wait_for_browser_port("127.0.0.1", 1234, Mock(returncode=3))

    async def test_browser_startup_deadline_is_bounded(self):
        with self.assertRaisesRegex(BrowserLaunchError, "DevTools"):
            await _wait_for_browser_port("127.0.0.1", 1234, Mock(returncode=None), timeout=0)

    async def test_launch_uses_virtual_screen_without_changing_parent_display(self):
        raw = Mock(pid=12345, returncode=0, wait=AsyncMock(return_value=0),
                   stderr=Mock(read=AsyncMock(return_value=b"")))
        with patch("linux_background.shutil.which", side_effect=lambda name: "/usr/bin/" + name), \
             patch("linux_background.asyncio.create_subprocess_exec", new_callable=AsyncMock,
                   return_value=raw) as spawn, \
             patch.dict(os.environ, {"DISPLAY": ":7", "WAYLAND_DISPLAY": "wayland-0"}):
            display = LinuxBackgroundDisplay()
            try:
                process = await display.start_process_async("/usr/bin/chromium", ["--remote-debugging-port=1234"])
                self.assertEqual(process.pid, 12345)
                arguments = spawn.call_args.args
                self.assertEqual(arguments[0], "/usr/bin/xvfb-run")
                self.assertIn("--auto-servernum", arguments)
                self.assertIn("--server-args=-screen 0 1920x1080x24 -nolisten tcp", arguments)
                self.assertIn("--ozone-platform=x11", arguments)
                self.assertIn("/usr/bin/chromium", arguments)
                self.assertNotIn("--headless", " ".join(arguments))
                self.assertTrue(spawn.call_args.kwargs["start_new_session"])
                self.assertNotIn("WAYLAND_DISPLAY", spawn.call_args.kwargs["env"])
                self.assertEqual(os.environ["DISPLAY"], ":7")
                self.assertEqual(os.environ["WAYLAND_DISPLAY"], "wayland-0")
            finally:
                await display.aclose()
                await display.aclose()

    async def test_forced_close_cleans_only_its_own_process_group(self):
        raw = Mock(pid=12345, returncode=None, stderr=Mock(read=AsyncMock(return_value=b"")))
        async def wait():
            raw.returncode = -15
            return -15
        raw.wait = AsyncMock(side_effect=wait)
        process = _LinuxProcess(raw)
        with patch("linux_background.os.killpg", create=True) as killpg, \
             patch("linux_background.signal.SIGKILL", 9, create=True):
            await process.aclose()
        self.assertEqual(killpg.call_args_list[0].args, (12345, signal.SIGTERM))
        self.assertEqual(killpg.call_args_list[1].args, (12345, 9))
        raw._transport.close.assert_called_once()
        self.assertTrue(process._reader.done())

    async def test_shutdown_timeout_kills_process_group(self):
        raw = Mock(pid=12345, returncode=None, stderr=Mock(read=AsyncMock(return_value=b"")),
                   wait=AsyncMock(side_effect=[asyncio.TimeoutError(), -9]))
        process = _LinuxProcess(raw)
        with patch("linux_background.os.killpg", create=True) as killpg, \
             patch("linux_background.signal.SIGKILL", 9, create=True):
            await process.aclose()
        self.assertEqual(killpg.call_args_list[-1].args, (12345, 9))
        self.assertEqual(raw.wait.await_count, 2)

    async def test_stderr_is_drained_and_bounded(self):
        raw = Mock(pid=12345, returncode=1, wait=AsyncMock(return_value=1),
                   stderr=Mock(read=AsyncMock(side_effect=[b"a" * 4096, b"b" * 4096, b"failure", b""])))
        process = _LinuxProcess(raw)
        await process._reader
        self.assertEqual(len(process.stderr.data), 8192)
        self.assertEqual(await process.stderr.read(7), b"failure")
        await process.aclose()


class LinuxClientTests(unittest.TestCase):
    def test_manual_login_without_display_has_actionable_error(self):
        client = SungnamClient("unused-profile", manual_login=True)
        try:
            with patch("sungnam_client.sys.platform", "linux"), patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(BrowserLaunchError, "DISPLAY.*--sn-background"):
                    client._run(client._start_browser())
        finally:
            client.close()

    def test_linux_background_uses_configured_browser_and_attaches(self):
        client = SungnamClient("unused-profile", background=True, browser_executable="/usr/bin/chromium")
        process = Mock(pid=12345, returncode=0)
        desktop = Mock(start_process_async=AsyncMock(return_value=process), aclose=AsyncMock())
        browser = Mock(_process=None, start=AsyncMock(), aclose=AsyncMock())
        browser.main_tab.activate = AsyncMock()
        try:
            with patch("sungnam_client.sys.platform", "linux"), \
                 patch("sungnam_client._wait_for_browser_port", new_callable=AsyncMock), \
                 patch("sungnam_client.create_background_desktop", return_value=desktop), \
                 patch("sungnam_client.uc.Config") as config, \
                 patch("sungnam_client.uc.Browser", return_value=browser):
                client._run(client._start_browser())
                self.assertEqual(config.call_args.kwargs["browser_executable_path"], "/usr/bin/chromium")
                self.assertIs(config.call_args.kwargs["headless"], False)
                self.assertIs(browser._process, process)
                browser.start.assert_awaited_once()
        finally:
            client.close()
        desktop.aclose.assert_awaited_once()

    def test_missing_browser_reports_install_and_override(self):
        client = SungnamClient("unused-profile", background=True)
        try:
            with patch("sungnam_client.uc.Config", side_effect=FileNotFoundError("not found")):
                with self.assertRaisesRegex(BrowserLaunchError, "Chrome/Chromium.*--sn-browser"):
                    client._run(client._start_browser())
        finally:
            client.close()

    def test_chrome_bin_environment_is_used(self):
        with patch.dict(os.environ, {"CHROME_BIN": "/opt/chrome/chrome"}):
            client = SungnamClient("unused-profile")
            try:
                self.assertEqual(client.browser_executable, "/opt/chrome/chrome")
            finally:
                client.close()


if __name__ == "__main__":
    unittest.main()
