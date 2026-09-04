# News Agent

CNBC와 Reuters 기사 후보를 수집하고, 검증한 본문을 한국어로 분석해 Telegram으로
전달하는 로컬 뉴스 비서다. Python 3.12 이상, macOS, Google Chrome, 인증된 로컬
Codex CLI를 사용한다.

## 로컬 설정

```sh
uv sync
cp config/cnbc-business.example.toml config/cnbc-business.toml
cp config/reuters.example.toml config/reuters.toml
chmod 600 config/cnbc-business.toml config/reuters.toml
```

이미 실행용 설정이 있다면 복사 명령으로 덮어쓰지 않는다. 새로 만든 설정의
`YOUR_TELEGRAM_CHAT_ID`와 `YOUR_TELEGRAM_BOT_ACCOUNT`를 자신의 값으로 바꾼다.
두 source는 같은 Telegram target ID와 Keychain 항목을 사용한다.

봇 토큰은 macOS 키체인 접근 앱에서 설정에 지정한 service/account의 일반 암호로
저장한다. 설정 파일에는 토큰 자체를 넣지 않는다. 환경변수를 사용하려면 Keychain
설정 두 항목 대신 `bot_token_env = "NEWS_AGENT_TELEGRAM_BOT_TOKEN"`을 지정한다.
`.env` 파일은 자동으로 읽지 않는다.

```sh
.venv/bin/news-agent --help
```

실행 정책과 명령은 [운영 계약](NEWS_AGENT_WORKFLOW.md), 자동 실행 설치는
[LaunchAgent 안내](launchd/README.md)를 따른다. LaunchAgent 템플릿의
`/Users/YOUR_USER/Desktop/news_agent`는 실제 저장소 경로로,
`/Users/YOUR_USER`와 `YOUR_USER:staff`는 실제 사용자 경로와 계정으로 바꿔야 한다.

## 저장소에 포함하지 않는 정보

- 실제 `config/*.toml`과 `.env` 등 인증·개인 설정
- `data/`의 기사 본문, 분석, DB, 선호 memory, 브라우저 profile과 로그인 세션
- 로그, 인증 파일, 로컬 작업 기록과 백업

GitHub에는 `*.example.toml` 예제만 포함한다. `.gitignore`는 이미 추적 중인 파일이나
과거 커밋을 지우지 않으므로 업로드 전에 파일 목록과 전송할 Git 기록을 확인한다.
이 저장소의 첫 업로드는 개인 식별자가 있던 기존 로컬 기록과 분리한 새 커밋이다.
기존 로컬 `master` 브랜치와 `.evidence/` 백업은 업로드 대상이 아니다.

비공개 저장소로 운영하고, push 전에는 Gitleaks로 전송할 브랜치를 검사한다.
검사 통과가 모든 보안 문제의 부재를 보장하지는 않는다.
