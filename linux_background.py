"""Linux에서 GUI Chrome과 팝업을 Xvfb 가상 화면 안에서 실행한다."""

import asyncio
import os
import shutil
import signal


class _CapturedStderr:
    def __init__(self):
        self.data = bytearray()

    async def read(self, size=-1):
        return bytes(self.data if size < 0 else self.data[-size:])


class _LinuxProcess:
    """xvfb-run과 그 자식만 종료하고 stderr가 가득 차는 것을 방지한다."""
    def __init__(self, process):
        self._process = process
        self.pid = process.pid
        self.stderr = _CapturedStderr()
        self._reader = asyncio.create_task(self._capture_stderr())
        self._group_signalled = False
        self._transport = self

    async def _capture_stderr(self):
        while chunk := await self._process.stderr.read(4096):
            self.stderr.data.extend(chunk)
            del self.stderr.data[:-8192]

    @property
    def returncode(self):
        return self._process.returncode

    async def wait(self):
        return await self._process.wait()

    def _signal_group(self, sig):
        # start_new_session=True로 생성한 이 실행의 프로세스 그룹에만 전송한다.
        try:
            os.killpg(self.pid, sig)
        except ProcessLookupError:
            pass

    def terminate(self):
        if self.returncode is None:
            self._group_signalled = True
            self._signal_group(signal.SIGTERM)

    def kill(self):
        if self.returncode is None or self._group_signalled:
            self._signal_group(signal.SIGKILL)
            self._group_signalled = False

    def close(self):
        transport = getattr(self._process, "_transport", None)
        if transport is not None:
            transport.close()
        self._reader.cancel()

    async def aclose(self):
        try:
            if self.returncode is None:
                self.terminate()
                try:
                    await asyncio.wait_for(self.wait(), timeout=3)
                except asyncio.TimeoutError:
                    self.kill()
                    await asyncio.wait_for(self.wait(), timeout=2)
        finally:
            # 그룹의 셸이 먼저 종료해도 강제 종료 경로에서는 자식을 정리한다.
            if self._group_signalled:
                self.kill()
            self.close()
            await asyncio.gather(self._reader, return_exceptions=True)


class LinuxBackgroundDisplay:
    def __init__(self):
        required = {name: shutil.which(name) for name in ("xvfb-run", "Xvfb", "xauth")}
        missing = [name for name, path in required.items() if not path]
        if missing:
            raise OSError(
                f"Linux 가상 화면 도구가 없습니다: {', '.join(missing)}. "
                "Debian/Ubuntu: sudo apt-get install xvfb xauth"
            )
        self._runner = required["xvfb-run"]
        self._process = None

    async def start_process_async(self, executable, arguments):
        if self._process is not None:
            raise RuntimeError("가상 화면의 브라우저가 이미 시작되었습니다.")
        # xvfb-run이 빈 DISPLAY, Xauthority 생성/삭제, Xvfb 종료를 관리한다.
        # 전역 DISPLAY는 바꾸지 않아 다른 스레드/앱의 화면에 영향을 주지 않는다.
        environment = os.environ.copy()
        environment.pop("WAYLAND_DISPLAY", None)
        process = await asyncio.create_subprocess_exec(
            self._runner, "--auto-servernum", "--error-file=/dev/stderr",
            "--server-args=-screen 0 1920x1080x24 -nolisten tcp",
            str(executable), "--ozone-platform=x11", *arguments,
            env=environment, start_new_session=True,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._process = _LinuxProcess(process)
        return self._process

    async def aclose(self):
        if self._process is not None:
            await self._process.aclose()
            self._process = None
