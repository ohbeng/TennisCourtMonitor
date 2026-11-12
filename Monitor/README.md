# Monitor

성남·용인 테니스 코트 예약 현황을 수집하고 텔레그램으로 알림을 보내는 **모니터링 서비스**입니다.
Linux PC에서 서비스(systemd)로 실행하며, 수집 결과는 Render에 배포된 [Webpage](../Webpage/README.md)로
전송합니다. 이 프로그램은 웹 서버를 열지 않습니다.

## 역할

| 구분 | Monitor (이 폴더) | Webpage |
|---|---|---|
| 실행 위치 | 내 Linux PC (systemd 서비스) | Render (Docker) |
| 하는 일 | 성남(Chrome)·용인 수집, 텔레그램 알림, 결과 전송 | 전송받은 결과 보관·대시보드·JSON API |
| 필요한 것 | Chrome/Chromium, Xvfb, 예약 사이트 계정 | Python/Flask만 |
| 비밀 정보 | 예약 계정, 텔레그램 토큰, 전송 토큰 | 전송 토큰만 |

예약 사이트는 Monitor가 가정용 IP에서 접속하므로 Render의 데이터센터 IP 때문에 차단될 일이 없고,
Render에는 Chrome이 필요 없어 작은 인스턴스로 충분합니다.

## 주요 기능

- 🎾 성남 + 용인 실시간 코트 예약 현황 모니터링
- 📲 예약 가능 시 텔레그램 알림 (변경 감지 시에만 발송)
- 🌐 수집 결과를 Webpage로 전송 (변경 시 즉시, 변경이 없어도 60초마다 연결 신호)
- 🔄 다중 계정 지원 및 자동 순환

## 파일 구조

```
Monitor/
├── tennis_court_monitor_all.py   # 진입점: 성남·용인 수집 스레드 + 웹 전송
├── web_publisher.py              # Webpage로 스냅샷 전송 (HTTPS + Bearer 토큰)
├── sungnam_client.py             # 성남 로그인·시간표 수집
├── browser_background.py         # 운영체제별 브라우저 실행 선택
├── windows_background.py         # Windows 전용 백그라운드 작업 공간
├── linux_background.py           # Linux Xvfb 가상 화면
├── auth.example.txt              # 계정 설정 예시
├── auth.txt                      # 개인 계정 + 텔레그램 설정 (Git 제외)
├── NotifyTable.txt               # 텔레그램 알림 대상 코트 정의
├── TELEGRAM_SETUP.md             # 텔레그램 봇 설정 가이드
├── deploy/                       # systemd 서비스 예시
├── requirements.txt
└── tests/                        # 수집·로그인·브라우저·전송 회귀 테스트
```

Chrome 프로필과 실행 로그는 실행 중 자동으로 생성되며 Git에서 제외됩니다.

---

## 설정 방법

### 1. 계정 설정 (`auth.txt`)

`auth.example.txt`를 `auth.txt`로 복사한 뒤 다음 형식으로 설정합니다.
기존 `auth.txt`가 있다면 `Monitor/` 폴더로 옮겨 그대로 사용하세요.

```
[sungnam]
계정ID1,비밀번호1
계정ID2,비밀번호2

[yongin]
계정ID1,비밀번호1
계정ID2,비밀번호2

[Telegram]
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

텔레그램 봇 설정 방법은 [TELEGRAM_SETUP.md](TELEGRAM_SETUP.md) 참고.

---

### 2. 관심코트 / 텔레그램 알림 설정 (`NotifyTable.txt`)

`NotifyTable.txt`에 적은 시간대는 모두 **관심코트**로 조회·표시됩니다.
시간대 뒤에 `(T)`를 붙인 것만 **변동 시 텔레그램 알림**을 보냅니다. `(T)`가 없으면 조회만 합니다.
`18:00~(T)`, `18:00~ (T)`, `All (T)`처럼 공백이 있어도 됩니다.

**성남** — FAC 코드 기반:

```
[sungnam]

FAC26(탄천실내)
주중
07:00 ~ 08:50
19:00 ~ 20:50 (T)
주말
All (T)

FAC61(수내)
주중
18:00 ~ 20:00
20:00 ~ 22:00
주말
All
```

- `#`으로 시작하는 줄은 비활성(주석)
- `All` = 해당 요일 전체 시간대

**용인** — 구(區) 기반:

```
[yongin]

기흥구
주중
~09:00           ← 조회만
18:00~(T)        ← 조회 + 텔레그램
주말
All

처인구        ← 규칙 없음 = 조회·알림 제외
```

- `HH:MM~` = 시작시간 ≥ HH:MM
- `~HH:MM` = 종료시간 ≤ HH:MM
- 규칙이 없는 구는 조회·알림 대상 제외

알림은 `(T)`가 붙은 시간대의 예약 가능 목록이 직전과 달라질 때 보냅니다.
목록이 모두 사라졌다가 취소로 다시 열린 경우에도 다시 보냅니다.

---

### 3. 조회 범위

용인은 `NotifyTable.txt [yongin]`의 구·시간 조건(`(T)` 유무와 상관없이 전부)을 조회 필터로 사용합니다.
성남은 `Sungnam/MonitoringTable.txt`가 있으면 해당 시설을 조회하고, 없으면
`NotifyTable.txt [sungnam]`의 시설·시간 조건(`(T)` 유무와 상관없이 전부)을 사용합니다.

---

## 실행 방법

### 모니터 실행 (성남 + 용인)

```bash
cd Monitor
python -m pip install -r requirements.txt
python tennis_court_monitor_all.py
```

Webpage로 결과를 보내려면 [Webpage로 결과 전송](#webpage로-결과-전송)을 설정하세요.
설정하지 않으면 텔레그램 알림만 동작합니다.

- 성남·용인: 한 번 조회를 마친 뒤 **120~300초(2~5분) 사이에서 무작위로** 쉬고 다시 조회
  (`tennis_court_monitor_all.py`의 `POLL_MIN_SECONDS` / `POLL_MAX_SECONDS`)
- 성남: 페이지 이동·클릭·로그인 입력 앞과 시설·날짜 조회 사이에 **1~4초 무작위 지연**
  (`sungnam_client.py`의 `HUMAN_DELAY_RANGE`)

성남은 **Windows와 Linux에서 기본으로 Chrome을 백그라운드 실행하고 저장된 계정으로 자동 로그인**합니다.
`auth.txt [sungnam]` 또는 계정 환경변수의 아이디·비밀번호를 로그인 입력란에 입력하고
로그인 버튼을 누릅니다. 기존 로그인 세션이 유효하면 다시 제출하지 않고 재사용합니다.
Chrome과 시간표 팝업은 Windows에서는 보이지 않는 별도 작업 공간, Linux에서는 Xvfb
가상 화면에서 실행되어 다른 작업의 화면이나 포커스를 가져오지 않습니다.
`--sn-auto-login`을 붙여도 같은 동작입니다.
로그인과 시간표 처리에는 일반 Chrome을 그대로 사용합니다.

화면을 보면서 확인하려면 다음처럼 실행하세요.

```bash
python tennis_court_monitor_all.py --sn-show-browser
```

사이트에서 추가 확인을 요구하거나 자동 로그인을 차단할 때는 직접 로그인 옵션을
사용할 수 있습니다.

```bash
python tennis_court_monitor_all.py --sn-manual-login
```

열린 Chrome 창에서 직접 로그인하세요. 로그인 완료를 확인한 뒤 같은 브라우저
세션으로 시간표를 수집하고 다음 조회에도 세션을 재사용합니다. 로그인 대기 시간이
지나도 Chrome을 닫거나 로그인 페이지를 다시 요청하지 않고 계속 기다립니다.
직접 로그인 옵션은 Chrome 창을 화면에 표시합니다. 이 창은 모니터링하는 동안 유지해야 합니다.
이 옵션은 사이트의 접근 차단을
해제하는 기능이 아니며, 사이트가 일반 로그인도 제한하면 해당 안내를 확인해야 합니다.
시간표는 사이트의 시설 목록과 달력을 거쳐 ‘다른 코트 시간표’ 창에서 읽습니다.
조회한 시간표 창은 자동으로 닫습니다.

성남 조회 실패는 Webpage에 오류로 표시되며, 마지막으로 성공한 결과와 갱신 시각을
유지합니다. Chrome이 응답하지 않아 로그인(180초) 또는 시간표 1건 조회(300초)가 제한 시간을
넘기면 해당 작업을 취소하고 Chrome을 종료한 뒤, 다음 주기에 새 Chrome으로 다시 로그인해
조회합니다. 자동 로그인 실패·차단 시에는 반복 제출을 중단합니다. 직접 로그인 모드에서는
사용자가 조치할 때까지 기다립니다. 사이트가 추가 확인을 요구하면 프로그램이 대신
해제하지 않습니다. 용인 모니터링은 계속됩니다.

성남 시간표는 시설·날짜별로 조회가 끝나는 즉시 스냅샷에 반영합니다. 도중에 차단돼도
이미 읽은 시간표는 유지하고, `확인 완료 5/16`, `미수집 11건`처럼 조회 범위를 표시합니다.
실패한 구간에 이전 성공 자료가 있으면 `(이전 조회)`로 구분하며, 실제 조회 결과가
0건인 구간과 아직 조회하지 못한 구간을 구분합니다.

매 조회는 아직 읽지 못했거나 가장 오래된 시설·날짜부터 시작하므로, 도중에 끊겨도
다음 조회에서 나머지 구간을 이어서 채웁니다.

용인 목록 GET 요청은 프록시 연결 끊김과 일시적인 서버 오류를 최대 3회 재시도합니다.
로그인 POST는 읽기 오류나 HTTP 오류로 재제출하지 않습니다. 목록 수집 도중 실패하면
앞부분만 수집한 목록을 정상 결과로 게시하지 않고, 이전 성공 결과와 오류를 표시합니다.

회귀 테스트는 `Monitor/` 폴더에서 `python -m unittest discover -s tests -v`로 실행할 수 있습니다.

프로그램을 `Ctrl+C` 또는 Linux의 `SIGTERM`으로 종료하면 성남 브라우저 작업을 취소하고 모니터 전용 Chrome도
함께 종료합니다. 종료 로그가 나온 뒤 다시 실행하세요. `Chrome 연결 실패`가 나오면
오류에 표시된 프로필을 사용하는 이전 모니터 또는 모니터 전용 Chrome 창이 남았는지
확인하세요. 시작 실패 시에는 생성 중이던 브라우저를 정리하고 반복 시작을 중단합니다.
백그라운드 작업 공간도 Chrome 종료 후 함께 정리합니다.

### Linux 설치 및 실행 (모니터)

Python 3.10 이상과 Linux용 Chrome 또는 Chromium이 필요합니다. Windows용 `chrome.exe`를
복사할 필요는 없습니다. 그래픽 데스크톱이 없는 서버에서도 Xvfb로 실행할 수 있습니다.
아래 설치 예시는 Debian/Ubuntu 기준입니다.

```bash
sudo apt-get update
sudo apt-get install -y python3-venv xvfb xauth fonts-noto-cjk
```

브라우저는 다음 중 한 가지를 설치하세요.

- Debian: `sudo apt-get install -y chromium`
- Ubuntu/Debian: [Chrome 공식 설치 안내](https://support.google.com/chrome/answer/95346?hl=ko)에
  따라 CPU에 맞는 Linux용 `.deb` 패키지를 설치합니다. 내려받은 파일은
  `sudo apt-get install ./다운로드한파일.deb`로 설치할 수 있습니다.

`Monitor/` 폴더에서 `auth.txt`와 `NotifyTable.txt`를 준비한 뒤 실행합니다.
프로그램은 일반 사용자 계정으로 실행하세요.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python tennis_court_monitor_all.py
```

의존성은 `requirements.txt`에 모았습니다. Linux에서도 기본 동작은 저장된 계정의 자동
로그인이며, Chrome은 `headless` 대신 Xvfb 안에서 일반 GUI 모드로 실행합니다.
Xvfb는 프로그램이 자동으로 시작하므로 명령 앞에 `xvfb-run`을 붙이지 않아도 됩니다.
가상 화면 번호와 인증 파일의 생성·정리는 [xvfb-run](https://manpages.debian.org/bookworm/xvfb/xvfb-run.1.en.html)이 관리합니다.

Chrome/Chromium은 자동 탐색합니다. 다른 경로에 설치했다면 다음 중 하나로 지정하세요.

```bash
python tennis_court_monitor_all.py --sn-browser /usr/bin/chromium
# 또는
CHROME_BIN=/usr/bin/google-chrome python tennis_court_monitor_all.py
```

로그인 프로필은 Windows의 `.nodriver_profile`, Linux의 `.nodriver_profile_linux`로 분리합니다.
필요하면 `--sn-profile /원하는/프로필폴더`로 변경할 수 있습니다. Windows의 Chrome 프로필을
Linux로 복사하지 말고, 기존 `auth.txt`의 계정으로 Linux에서 새로 자동 로그인하세요.

Linux에서 `--sn-show-browser` 또는 `--sn-manual-login`을 쓰려면 `DISPLAY` 또는
`WAYLAND_DISPLAY`가 설정된 실제 데스크톱 세션이 필요합니다. 화면 없는 SSH 세션에서
직접 로그인 옵션을 사용하면 필요한 화면 설정을 오류 메시지로 안내합니다.

---

## Webpage로 결과 전송

Monitor는 수집 결과를 `POST {WEBPAGE_URL}/api/ingest`로 보냅니다. 두 환경변수가 모두 있어야 전송합니다.

| 환경변수 | 설명 |
|---|---|
| `WEBPAGE_URL` | Render 서비스 주소. 원격 주소는 `https://`만 허용합니다 (`--webpage-url`로도 지정 가능) |
| `WEBPAGE_INGEST_TOKEN` | Webpage에 설정한 `WEBPAGE_INGEST_TOKEN`과 같은 값 |

- 스냅샷이 바뀌면 10초 안에, 바뀌지 않아도 60초마다 전송합니다. Webpage가 5분(기본) 넘게
  아무것도 받지 못하면 대시보드에 “모니터와 연결이 끊겼습니다”를 표시합니다.
- 전송이 실패해도 수집과 텔레그램 알림은 계속됩니다. 실패/복구는 `[WEB]` 로그로 상태가 바뀔 때만 기록합니다.
- 토큰 거부 시 로그에 `Webpage가 토큰을 거부했습니다`가 남습니다.
- 성남을 끄고 용인만 돌리려면 `--no-sungnam` 또는 `SUNGNAM_ENABLED=false`를 사용하세요.

토큰 생성 예시:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

---

## Linux 서비스로 실행 (systemd)

[deploy/tennis-monitor.service](deploy/tennis-monitor.service)와
[deploy/monitor.env.example](deploy/monitor.env.example)을 사용합니다.

```bash
# 1) 설치 (위의 'Linux 설치 및 실행'의 apt 패키지, venv, auth.txt 준비 후)
cd ~/TennisCourtMonitor/Monitor
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt

# 2) 환경변수 파일 (토큰은 root만 읽을 수 있게 보관)
sudo install -m 600 -o root -g root deploy/monitor.env.example /etc/tennis-monitor.env
sudo nano /etc/tennis-monitor.env

# 3) 서비스 파일의 YOUR_USER와 경로를 수정한 뒤 등록
sudo cp deploy/tennis-monitor.service /etc/systemd/system/
sudo nano /etc/systemd/system/tennis-monitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now tennis-monitor

# 로그 확인 / 재시작 / 중지
journalctl -u tennis-monitor -f
sudo systemctl restart tennis-monitor
sudo systemctl stop tennis-monitor
```

중지(`SIGTERM`)하면 성남 브라우저 작업을 취소하고 Chrome·Xvfb를 정리한 뒤 종료합니다.
로그 파일은 `Monitor/log_all/`에도 저장됩니다.

---

## 문제 해결

### SSL 인증서 오류

```
[SSL: CERTIFICATE_VERIFY_FAILED]
```

→ 코드에서 자동 처리됩니다 (`verify=False`)

### 로그인 실패

```
❌ 로그인 실패
```

→ `auth.txt`의 계정 정보를 확인하세요

### 텔레그램 알림 미전송

→ `auth.txt [Telegram]` 섹션의 `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` 확인  
→ [TELEGRAM_SETUP.md](TELEGRAM_SETUP.md) 참고

---

## 라이선스

MIT License
