# Webpage

테니스 코트(성남·용인) 예약 현황을 보여 주는 **웹 대시보드**입니다. Render에 Docker로 배포합니다.
수집은 하지 않고, [Monitor](../Monitor/README.md)가 보내 준 최신 결과만 보관·표시합니다.

```
Monitor (Linux PC) ──POST /api/ingest (HTTPS + Bearer 토큰)──▶ Webpage (Render) ◀── 브라우저
```

## 파일 구조

```
Webpage/
├── app.py               # Flask 앱: 대시보드, JSON API, 수신 엔드포인트
├── templates/index.html # 대시보드 화면 (⭐관심 코트 / ✅예약가능 / 📊전체 현황)
├── requirements.txt     # flask, gunicorn
├── Dockerfile           # gunicorn 1 worker (상태를 메모리에 보관)
├── .dockerignore
└── tests/               # API·인증·만료 처리 테스트
```

## 엔드포인트

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/` | 대시보드 |
| GET | `/api/sungnam`, `/api/yongin` | 마지막으로 받은 결과 (JSON) |
| POST | `/api/ingest` | Monitor 전용. `Authorization: Bearer <WEBPAGE_INGEST_TOKEN>` 필요 |
| GET | `/healthz` | 상태 확인 (Render Health Check Path) |

- 처음 데이터를 받기 전에는 `status: "waiting"`(“수집 중…”)으로 응답합니다.
- 마지막 수신이 `STALE_SECONDS`(기본 300초)보다 오래되면 데이터는 유지하되
  `status: "stale"`과 “모니터에서 N분째 데이터를 받지 못했습니다” 오류를 함께 보여 줍니다.
- 대시보드는 처음 열 때 한 번 읽고, 이후에는 **새로고침 버튼을 누른 도시만** 최신 결과를 읽습니다.
  새로고침은 Webpage에 보관된 결과를 읽을 뿐 예약 사이트에 요청을 보내지 않습니다.

## 환경변수

| 이름 | 필수 | 설명 |
|---|---|---|
| `WEBPAGE_INGEST_TOKEN` | 예 | Monitor가 보내는 토큰과 같은 값. 비어 있으면 `/api/ingest`는 503으로 비활성화됩니다 |
| `STALE_SECONDS` | 아니오 | 마지막 수신 후 “연결 끊김”으로 표시할 시간(초), 기본 300 |
| `STATE_FILE` | 아니오 | 마지막 결과를 저장할 파일 (기본: 임시 폴더). 재시작 직후 Monitor의 다음 전송 전까지 이전 결과를 보여 주는 용도이며, Render 무료/기본 디스크는 재배포 때 지워집니다 |
| `PORT` | 아니오 | Render가 자동 주입 |

토큰은 길고 무작위인 값을 쓰세요.

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Render 배포

1. Render → **New + → Web Service** → 이 저장소(`master` 브랜치) 선택.
2. **Language: Docker**, **Root Directory: `Webpage`** (Dockerfile Path는 `./Dockerfile`).
   Build/Start Command는 비워 둡니다.
3. **Environment**에 `WEBPAGE_INGEST_TOKEN`을 추가합니다. 같은 값을 Monitor의
   `/etc/tennis-monitor.env`(`WEBPAGE_INGEST_TOKEN`)에도 넣습니다.
4. **Health Check Path**를 `/healthz`로 설정합니다.
5. 인스턴스는 가장 작은 것으로 충분합니다 (Chrome이 없습니다).
6. 배포 후 Monitor의 `WEBPAGE_URL`에 서비스 주소(`https://<이름>.onrender.com`)를 넣고
   Monitor를 재시작합니다.

> 기존에 Chrome을 포함한 Docker 서비스를 쓰고 있었다면 같은 서비스의 **Root Directory를
> `Webpage`로 바꾸고 환경변수에서 예약 계정·텔레그램 값을 삭제**하세요. 새 서비스를 만들어도 됩니다.
> Render 무료 인스턴스는 유휴 상태에서 잠들 수 있어 첫 접속이 느릴 수 있습니다.

## 로컬 실행·테스트

```bash
cd Webpage
python -m pip install -r requirements.txt
WEBPAGE_INGEST_TOKEN=dev-token PORT=8000 python app.py     # http://localhost:8000
python -m unittest discover -s tests -v
```

Windows PowerShell에서는 `$env:WEBPAGE_INGEST_TOKEN='dev-token'; python app.py`처럼 환경변수를 지정합니다.
Monitor를 같은 PC에서 시험하려면 `WEBPAGE_URL=http://localhost:8000`을 사용할 수 있습니다
(원격 주소는 https만 허용).

## 보안 메모

- 대시보드와 읽기 API는 공개입니다. 예약자 이름(`reservation_name`) 등이 결과에 포함될 수 있으므로
  공개해도 되는지 확인하세요. 필요하면 Render 앞단에 인증을 두세요.
- 수신 토큰은 상수 시간 비교로 검사하며, 요청 본문은 최대 8MB로 제한합니다.
- 예약 사이트 계정과 텔레그램 토큰은 Monitor에만 있고 Webpage에는 필요 없습니다.
