"""운영체제에 맞는 모니터 전용 브라우저 작업 공간을 선택한다."""

import os
import sys


def supports_background():
    return sys.platform == "win32" or sys.platform.startswith("linux")


def default_profile_directory(base_directory):
    # Windows 프로필의 암호화된 쿠키/잠금 파일을 Linux에서 재사용하지 않는다.
    name = ".nodriver_profile_linux" if sys.platform.startswith("linux") else ".nodriver_profile"
    return os.path.join(base_directory, name)


def create_background_desktop():
    # 다른 운영체제의 네이티브 모듈을 import하지 않는다.
    if sys.platform == "win32":
        from windows_background import WindowsBackgroundDesktop
        return WindowsBackgroundDesktop()
    if sys.platform.startswith("linux"):
        from linux_background import LinuxBackgroundDisplay
        return LinuxBackgroundDisplay()
    raise OSError("이 운영체제에서는 --sn-show-browser 옵션을 사용하세요.")
