# TennisCourtMonitor

성남·용인 테니스 코트 예약 현황을 자동으로 모니터링하고 텔레그램으로 알림을 보내는 통합 모니터링 앱입니다.

## 주요 기능

- 🎾 성남 + 용인 실시간 코트 예약 현황 모니터링
- 📲 예약 가능 시 텔레그램 알림 (변경 감지 시에만 발송)
- 🌐 통합 웹 대시보드 (http://localhost:8000)
- 🔄 다중 계정 지원 및 자동 순환
- 📋 3섹션 UI: ⭐관심 코트 / ✅전체 예약가능 / 📊전체 현황

## 파일 구조

```
TennisCourtMonitor/
├── tennis_court_monitor_all.py   # 메인 통합 모니터 (포트 8000)
├── auth.txt                      # 성남·용인 계정 + 텔레그램 설정
├── NotifyTable.txt               # 텔레그램 알림 대상 코트 정의
├── MonitoringTable.txt           # 용인 스캔 대상 코트 필터
├── email_config.txt              # 이메일 알림 설정 (선택)
├── TELEGRAM_SETUP.md             # 텔레그램 봇 설정 가이드
├── requirements.txt
└── Yongin/
    └── tennis_court_monitor_yongin.py  # 용인 단독 실행용
```

> 성남 단독 실행: `Sungnam/tennis_court_sungnam.py`

---

## 설정 방법

### 1. 계정 설정 (`auth.txt`)

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

### 2. 텔레그램 알림 대상 설정 (`NotifyTable.txt`)

알림을 받을 코트를 정의합니다.

**성남** — FAC 코드 기반:

```
[sungnam]

FAC26(탄천실내)
주중
07:00 ~ 08:50
19:00 ~ 20:50
주말
All

FAC61(수내)
주중
18:00 ~ 20:00
20:00 ~ 22:00
주말
All
```

- `#`으로 시작하는 줄은 비활성(주석)
- `All` = 해당 요일 전체 시간대 알림

**용인** — 구(區) 기반:

```
[yongin]

기흥구
주중
18:00~
주말
All

처인구        ← 규칙 없음 = 알림 제외
```

- `HH:MM~` = 시작시간 ≥ HH:MM
- `~HH:MM` = 종료시간 ≤ HH:MM
- 규칙이 없는 구는 알림 대상 제외

---

### 3. 용인 스캔 필터 설정 (`MonitoringTable.txt`)

조회 자체를 제한할 구/시간대를 설정합니다. (규칙 없는 구는 조회 제외)

```
[yongin_monitor]

기흥구
주중
~08:00       ← 종료시간 08:00 이하
18:00~       ← 시작시간 18:00 이상
주말
All

처인구        ← 규칙 없음 = 조회 제외
```

> **MonitoringTable** (스캔 필터) vs **NotifyTable** (알림 규칙):  
> MonitoringTable에 없는 코트는 조회조차 하지 않습니다.  
> NotifyTable은 조회된 코트 중 텔레그램 알림을 보낼 대상만 추가로 필터링합니다.

---

## 실행 방법

### 통합 모니터 (성남 + 용인)

```bash
python tennis_court_monitor_all.py
```

웹 대시보드: http://localhost:8000

- 성남: 90초 간격 모니터링
- 용인: 300초 간격 모니터링

### 용인 단독 실행

```bash
python Yongin/tennis_court_monitor_yongin.py
```

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
