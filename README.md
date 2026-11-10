# TennisCourtMonitor

성남·용인 테니스 코트 예약 현황을 모니터링하고 텔레그램으로 알리며, 웹 대시보드로 보여 주는 프로젝트입니다.
역할에 따라 두 개의 독립된 앱으로 나뉩니다.

```
TennisCourtMonitor/
├── Monitor/    # Linux PC에서 서비스(systemd)로 실행: 수집 + 텔레그램 알림 + 결과 전송
└── Webpage/    # Render에 Docker로 배포: 결과 보관 + 대시보드 + JSON API
```

```
┌─ 내 Linux PC ──────────────┐          ┌─ Render ─────────────────┐
│ Monitor                    │  HTTPS   │ Webpage                  │
│  · 성남(Chrome/Xvfb) 수집  │ ───────▶ │  · POST /api/ingest      │ ◀── 브라우저
│  · 용인 수집               │  Bearer  │  · 대시보드 / JSON API   │
│  · 텔레그램 알림           │  토큰    │                          │
└────────────────────────────┘          └──────────────────────────┘
```

예약 사이트는 Chrome이 필요하고 데이터센터 IP를 차단할 수 있으므로 수집은 가정용 IP의 Linux PC에서,
Render에서는 Chrome 없이 화면만 제공합니다.

| 폴더 | 역할 | 문서 |
|---|---|---|
| [Monitor](Monitor/) | 수집·알림·전송, systemd 서비스 | [Monitor/README.md](Monitor/README.md) |
| [Webpage](Webpage/) | 대시보드, Render 배포 | [Webpage/README.md](Webpage/README.md) |

## 빠른 시작

1. **Webpage 배포 (Render)**: [Webpage/README.md](Webpage/README.md)의 *Render 배포* 절차를 따르고
   `WEBPAGE_INGEST_TOKEN`을 설정합니다.
2. **Monitor 설치 (Linux PC)**: [Monitor/README.md](Monitor/README.md)의 설치·`auth.txt` 설정 후,
   `WEBPAGE_URL`과 같은 `WEBPAGE_INGEST_TOKEN`으로 systemd 서비스를 등록합니다.

두 앱은 코드를 공유하지 않으며, 주고받는 데이터는 `POST /api/ingest`의 JSON 스냅샷
(`{"sent_at": ..., "snapshot": {"sungnam": {...}, "yongin": {...}}}`)뿐입니다.

## 테스트

```bash
cd Monitor && python -m unittest discover -s tests
cd Webpage && python -m unittest discover -s tests
```

## 라이선스

MIT License
