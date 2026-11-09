"""Chrome 전용 Windows 데스크톱. 사용자의 입력 데스크톱으로 전환하지 않는다."""

import asyncio
import ctypes
import os
import subprocess
import uuid
from ctypes import wintypes


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE),
    ]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


class _DesktopProcess:
    """nodriver의 종료 처리에 필요한 프로세스 인터페이스."""
    def __init__(self, kernel32, handle, pid):
        self._kernel32, self._handle, self.pid = kernel32, handle, pid
        self._exit_code = None
        self.stderr = None
        self._transport = self

    @property
    def returncode(self):
        if self._handle:
            code = wintypes.DWORD()
            if not self._kernel32.GetExitCodeProcess(self._handle, ctypes.byref(code)):
                raise ctypes.WinError(ctypes.get_last_error())
            self._exit_code = None if code.value == 259 else code.value  # STILL_ACTIVE
        return self._exit_code

    async def wait(self):
        while self.returncode is None:
            await asyncio.sleep(0.05)
        return self.returncode

    def terminate(self):
        if self._handle and self.returncode is None:
            if not self._kernel32.TerminateProcess(self._handle, 1):
                raise ctypes.WinError(ctypes.get_last_error())

    kill = terminate

    def close(self):
        if self._handle:
            if self.returncode is None:
                self.terminate()
                self._kernel32.WaitForSingleObject(self._handle, 5000)
            self._exit_code = self.returncode
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


class WindowsBackgroundDesktop:
    def __init__(self):
        if os.name != "nt":
            raise OSError("백그라운드 데스크톱은 Windows에서만 지원합니다.")
        self.name = f"TennisCourtMonitor_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.CreateDesktopW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        ]
        self._user32.CreateDesktopW.restype = wintypes.HANDLE
        self._user32.CloseDesktop.argtypes = [wintypes.HANDLE]
        self._user32.CloseDesktop.restype = wintypes.BOOL
        # DESKTOP_ALL_ACCESS. 보안 설명자는 현재 윈도 스테이션에서 상속한다.
        self._handle = self._user32.CreateDesktopW(self.name, None, None, 0, 0x000F01FF, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def start_process(self, executable, arguments):
        # Python subprocess의 STARTUPINFO는 lpDesktop을 전달하지 않으므로
        # Win32 구조체와 CreateProcessW를 직접 사용한다.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateProcessW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
            wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
            ctypes.POINTER(_StartupInfo), ctypes.POINTER(_ProcessInfo),
        ]
        kernel32.CreateProcessW.restype = wintypes.BOOL
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        startup = _StartupInfo()
        startup.cb = ctypes.sizeof(startup)
        startup.lpDesktop = self.name
        startup.dwFlags = 0x01 | 0x80  # STARTF_USESHOWWINDOW | STARTF_FORCEOFFFEEDBACK
        # Chrome은 전용 데스크톱 안에서 정상 표시한다. 사용자 화면으로 전환하지 않는다.
        startup.wShowWindow = 1  # SW_SHOWNORMAL
        info = _ProcessInfo()
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline([str(executable), *arguments]))
        if not kernel32.CreateProcessW(str(executable), command, None, None, False, 0,
                                       None, None, ctypes.byref(startup), ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        kernel32.CloseHandle(info.hThread)
        return _DesktopProcess(kernel32, info.hProcess, info.dwProcessId)

    async def start_process_async(self, executable, arguments):
        return self.start_process(executable, arguments)

    async def aclose(self):
        self.close()

    def close(self):
        if self._handle:
            if not self._user32.CloseDesktop(self._handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self._handle = None
