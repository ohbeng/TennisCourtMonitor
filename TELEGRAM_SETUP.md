# 텔레그램 알림 설정

1. 텔레그램의 [@BotFather](https://t.me/BotFather)에서 `/newbot`으로 봇을 생성하고 토큰을 받습니다.
2. 생성한 봇과 대화를 시작하고 메시지를 보냅니다.
3. `https://api.telegram.org/bot<봇토큰>/getUpdates` 응답의 `message.chat.id`를 확인합니다.
4. 프로젝트의 `auth.txt`에 다음 항목을 설정합니다.

```ini
[Telegram]
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
```

또는 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` 환경변수를 함께 설정합니다.
두 환경변수가 모두 있으면 파일보다 우선합니다. 설정이 없으면 알림을 보내지 않습니다.

알림 대상은 `NotifyTable.txt`의 `[sungnam]`, `[yongin]`에서 지정합니다.
조회한 예약 가능 목록이 직전 알림 목록과 달라질 때 알림을 보냅니다.
설정을 바꾼 뒤에는 모니터를 다시 실행하세요.

알림이 오지 않으면 봇과 대화를 시작했는지, 토큰과 Chat ID가 맞는지,
알림 조건에 해당하는 예약 가능 코트가 있는지 확인하세요.

토큰과 개인 계정이 들어 있는 `auth.txt`는 Git 커밋에서 제외됩니다.
