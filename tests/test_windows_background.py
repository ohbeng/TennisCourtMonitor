import asyncio
import unittest
from unittest.mock import Mock, patch

from windows_background import WindowsBackgroundDesktop, _DesktopProcess


class DesktopProcessTests(unittest.TestCase):
    def setUp(self):
        self.code = 259
        self.kernel = Mock()
        def get_code(handle, result):
            result._obj.value = self.code
            return True
        self.kernel.GetExitCodeProcess.side_effect = get_code
        self.process = _DesktopProcess(self.kernel, 123, 456)

    def tearDown(self):
        self.code = 0
        self.process.close()

    def test_exit_code_is_cached_after_handle_closes(self):
        self.assertIsNone(self.process.returncode)
        self.code = 0
        self.process.close()
        self.assertEqual(self.process.returncode, 0)
        self.process.close()
        self.kernel.CloseHandle.assert_called_once_with(123)

    def test_close_terminates_only_owned_running_process(self):
        def terminate(handle, status):
            self.assertEqual(handle, 123)
            self.code = status
            return True
        self.kernel.TerminateProcess.side_effect = terminate
        self.process.close()
        self.kernel.TerminateProcess.assert_called_once_with(123, 1)
        self.kernel.WaitForSingleObject.assert_called_once_with(123, 5000)
        self.assertEqual(self.process.returncode, 1)

    def test_async_wait_finishes_when_process_exits(self):
        async def check():
            asyncio.get_running_loop().call_soon(lambda: setattr(self, "code", 0))
            return await self.process.wait()
        self.assertEqual(asyncio.run(check()), 0)


class NativeStartupTests(unittest.TestCase):
    def test_create_process_receives_native_desktop_field(self):
        desktop = object.__new__(WindowsBackgroundDesktop)
        desktop.name = "monitor-private-desktop"
        kernel = Mock()
        def create_process(executable, command, process_attrs, thread_attrs, inherit,
                           flags, env, cwd, startup, info):
            self.assertEqual(startup._obj.lpDesktop, "monitor-private-desktop")
            self.assertEqual(startup._obj.wShowWindow, 1)
            self.assertEqual(executable, "C:/Program Files/Chrome/chrome.exe")
            self.assertIn('"C:/Program Files/Chrome/chrome.exe"', command.value)
            info._obj.hProcess, info._obj.hThread = 123, 789
            info._obj.dwProcessId = 456
            return True
        kernel.CreateProcessW.side_effect = create_process
        def get_code(handle, result):
            result._obj.value = 0
            return True
        kernel.GetExitCodeProcess.side_effect = get_code
        with patch("windows_background.ctypes.WinDLL", return_value=kernel, create=True):
            process = desktop.start_process("C:/Program Files/Chrome/chrome.exe", ["--user-data-dir=C:/test profile"])
        self.assertEqual(process.pid, 456)
        kernel.CloseHandle.assert_called_once_with(789)
        process.close()
        self.assertEqual(kernel.CloseHandle.call_count, 2)


if __name__ == "__main__":
    unittest.main()
