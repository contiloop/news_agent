# News Agent

CNBC와 Reuters의 새 기사를 찾아 한국어로 요약하고, 새로운 사건을 다룬 기사를
Telegram으로 전달하는 macOS용 개인 뉴스 비서입니다.

기사 본문을 확인한 뒤 Codex CLI로 제목 번역, 상세 요약, 사건 중복 판정을 수행합니다.
수집한 기사와 처리 이력은 로컬에 저장하며, Telegram 메시지에 남긴 반응은 Reuters
기사 후보를 선택할 때 참고할 관심사 기록에 반영합니다.

## 주요 기능

- **기사 수집과 선별:** CNBC의 뉴스 사이트맵과 Reuters의 분야별 페이지에서 기사 후보를 찾습니다. Reuters 후보는 관심 분야와 이전 반응을 참고해 선별합니다.
- **본문 확인:** 렌더링된 페이지의 주소, 제목, 본문 영역을 검증합니다. 본문을 확인할 수 없는 기사는 저장하거나 요약하지 않습니다.
- **한국어 요약과 알림:** 번역한 제목, 주요 사실을 담은 요약, 원문 링크를 Telegram으로 보냅니다.
- **사건 단위 중복 판정:** 이미 다룬 사건으로 판정된 기사는 기존 사건에 연결하고, 새 알림을 만들지 않습니다.
- **실패 처리와 재시도:** 기사 읽기, 분석, 알림 전송의 실패 이력을 저장하고 각 단계의 정책에 따라 재시도합니다.
- **자동 실행과 상태 감시:** macOS LaunchAgent로 작업을 주기적으로 실행하고, Reuters 작업의 장애와 복구 상태를 별도 감시 작업으로 확인합니다.

## 동작 방식

1. 설정한 뉴스 출처에서 기사 후보를 찾습니다.
2. 처리 이력과 선택 기준을 적용해 읽을 기사를 정합니다.
3. 전용 Chrome 프로필에서 본문을 확인하고 SQLite에 저장합니다.
4. Codex CLI로 한국어 제목과 요약을 생성하고, 기존 사건과의 관련성을 판정합니다.
5. 새로운 사건으로 판정된 기사의 알림을 Telegram으로 전송합니다.

각 명령은 정해진 작업을 한 번 수행한 뒤 종료합니다. `run-once`는 실행당 새 기사 최대
3건을 처리하며, 재시도할 작업과 대기 중인 알림도 정해진 한도 안에서 처리합니다.
주기적인 실행은 Python 내부 반복문이 아니라 LaunchAgent가 담당합니다.

### 기본 뉴스 출처

| 출처 | 기사 후보 수집 | 본문을 읽는 방식 |
| --- | --- | --- |
| CNBC Business | 뉴스 사이트맵을 조회하며, 기본 설정에서는 `Earnings` 키워드가 붙은 기사를 제외합니다. | 전용 Chrome을 헤드리스 모드로 실행합니다. |
| Reuters | Business, Markets, World, Technology 페이지에서 후보를 수집하고 관심사에 따라 선별합니다. | macOS에서 Chrome을 숨김 상태로 실행하고 CDP로 페이지를 제어합니다. |

사이트별 주소, 본문 선택자, 관심 분야와 처리 한도는 `config/`의 TOML 설정에서 조정합니다.

## 실행 환경

- macOS와 로그인된 사용자 데스크톱 세션이 필요합니다. Reuters의 숨김 실행과 LaunchAgent 운영은 이 환경을 기준으로 구현되어 있습니다.
- Python 3.12 이상과 `uv`가 필요합니다.
- Google Chrome이 설치되어 있어야 합니다. 예제 설정은 `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome` 경로를 사용합니다.
- 인증된 `codex` 실행 파일이 `PATH`에 있어야 합니다. 현재 코드는 `gpt-5.5` 모델과 `low` 추론 강도를 지정하며, 해당 모델과 코드에서 사용하는 CLI 옵션을 지원하는 환경이 필요합니다.
- Telegram 봇과 봇이 메시지를 보낼 수 있는 채팅이 필요합니다.

## 시작하기

### 1. 저장소와 실행 환경 준비

```sh
git clone https://github.com/contiloop/news_agent.git
cd news_agent
uv sync --locked
```

### 2. 개인 설정 만들기

```sh
cp -n config/cnbc-business.example.toml config/cnbc-business.toml
cp -n config/reuters.example.toml config/reuters.toml
chmod 600 config/cnbc-business.toml config/reuters.toml
```

`cp -n`은 기존 설정 파일을 덮어쓰지 않습니다. 복사한 두 파일에서 아래 값을 자신의
환경에 맞게 수정합니다.

| 설정 | 입력할 값 |
| --- | --- |
| `chat_id` | 알림을 받을 Telegram 채팅 ID입니다. `YOUR_TELEGRAM_CHAT_ID`를 바꿉니다. |
| `bot_token_keychain_account` | 봇 토큰을 저장할 키체인 항목의 계정 이름입니다. `YOUR_TELEGRAM_BOT_ACCOUNT`를 바꿉니다. |
| `browser_executable` | Google Chrome 실행 파일의 경로입니다. 기본 설치 경로와 다를 때 수정합니다. |

두 뉴스 출처에서 같은 봇과 채팅을 사용한다면 `notifications.targets`의 `id`, 채팅 ID,
키체인 설정을 동일하게 유지합니다. 예제 설정은 DB와 관심사 기록을 공유하되, Chrome
프로필은 출처마다 별도로 사용합니다.

### 3. Telegram 인증 설정

기본 설정은 macOS 키체인에서 봇 토큰을 읽습니다. **키체인 접근** 앱에서 다음 값으로
암호 항목을 만듭니다.

| 키체인 항목 | 값 |
| --- | --- |
| 이름 | `news-agent.telegram.bot-token` |
| 계정 | 설정 파일의 `bot_token_keychain_account`에 입력한 이름 |
| 암호 | Telegram 봇 토큰 |

LaunchAgent로 자동 실행하려면 같은 사용자 계정의 프로세스가 키체인 항목을
대화상자 없이 읽을 수 있어야 합니다.

환경변수를 사용하려면 각 설정 파일에서 `bot_token_keychain_service`와
`bot_token_keychain_account`를 제거하고, 같은 위치에 다음 항목을 넣습니다.

```toml
bot_token_env = "NEWS_AGENT_TELEGRAM_BOT_TOKEN"
```

실행할 프로세스의 환경에 해당 이름으로 봇 토큰을 설정해야 합니다. 키체인과 환경변수
설정은 함께 사용할 수 없으며, 프로그램은 `.env` 파일을 자동으로 읽지 않습니다.

### 4. 명령 확인과 첫 실행

명령 목록과 CNBC 기사 후보를 확인합니다.

```sh
.venv/bin/news-agent --help
.venv/bin/news-agent discover --config config/cnbc-business.toml
```

설정을 확인한 뒤 원하는 출처를 한 번 실행합니다. 아래 명령은 기사 수집부터 분석과
Telegram 알림 전송까지 수행합니다.

```sh
.venv/bin/news-agent run-once --config config/cnbc-business.toml
.venv/bin/news-agent run-once --config config/reuters.toml
```

두 출처가 DB를 공유하므로 순서대로 실행합니다. 같은 DB를 사용하는 `run-once`가 이미
진행 중이면 뒤에 실행한 작업은 `already_running` 상태로 종료됩니다.

## 자동 실행과 피드백

`launchd/`에는 CNBC 수집, Reuters 수집, Telegram 반응 수집, Reuters 상태 감시를 위한
네 가지 LaunchAgent 템플릿이 있습니다. 저장소를 복제하는 것만으로 자동 실행되지는
않습니다.

설치하기 전에 템플릿의 `/Users/YOUR_USER/Desktop/news_agent`를 실제 저장소 경로로,
`/Users/YOUR_USER`와 `YOUR_USER:staff`를 실제 사용자 경로와 계정으로 바꿉니다.
검증, 설치, 중지와 로그 관리 절차는 [LaunchAgent 운영 안내](launchd/README.md)를 따릅니다.

Telegram 반응을 수동으로 한 번 수집하려면 다음 명령을 사용합니다. 수집된 반응과
메시지의 연결 정보는 DB에 기록되고, 관심사 기록은 `data/memory/MEMORY.md`에 반영됩니다.

```sh
.venv/bin/news-agent telegram-feedback-once \
  --config config/reuters.toml --quiet-when-idle
```

## 데이터와 인증 정보

기사 본문, 요약, 처리 이력, 관심사 기록과 브라우저 프로필은 기본적으로 `data/`에
저장됩니다. 프로그램은 로컬에서 실행되지만 분석은 Codex CLI를 통해 수행하므로 기사와
분석에 필요한 문맥이 모델 서비스로 전달되며, 알림 내용은 Telegram으로 전송됩니다.

공개 저장소에는 코드, 문서와 예제 설정을 포함합니다. 다음 정보는 Git 추적 대상에서 제외합니다.

- 개인 설정인 `config/*.toml`과 `.env` 파일
- 기사 본문, DB, 요약과 관심사 기록을 포함한 `data/` 디렉터리
- 브라우저 로그인 세션, 로그, 인증 파일과 로컬 작업 기록

공유할 설정은 `*.example.toml`로 작성하고 실제 토큰이나 개인 식별자를 넣지 않습니다.
봇 토큰은 설정 파일에 직접 적지 않고 키체인 또는 환경변수로 관리합니다.

## 현재 제약

- 사이트 구조나 접근 정책이 바뀌면 기사 수집이 실패할 수 있습니다. CNBC의 구독 제한이나 Reuters의 접근 거부로 본문을 확인하지 못하면 해당 기사를 저장하지 않습니다.
- Reuters의 숨김 실행은 macOS와 Chrome 동작에 의존합니다. 다른 환경에서도 창이나 포커스가 바뀌지 않는다고 보장하지 않으므로 자동 실행 전에 확인해야 합니다.
- 요약과 사건 중복 판정은 모델 결과에 의존합니다. 모든 사건을 정확히 구분하거나 중복 알림을 완전히 방지한다고 보장하지 않습니다.
- Mac이 잠들거나 꺼져 있으면 정기 작업과 상태 감시도 실행되지 않습니다. Reuters 상태 감시 작업은 기사 접근 오류를 해결하거나 수집 작업을 자동 재시작하지 않습니다.
- 현재 저장소에는 자동 테스트 모음이 없습니다. 문법·설정 검사는 실제 기사 수집과 알림 수신을 검증하지 않으므로 실행 환경에서 별도로 확인해야 합니다.

## 문서와 코드

- [처리 절차와 운영 계약](NEWS_AGENT_WORKFLOW.md): 본문 검증, 사건 판정, 재시도와 각 명령의 동작을 설명합니다.
- [LaunchAgent 운영 안내](launchd/README.md): 자동 실행, 상태 확인과 로그 관리 절차를 설명합니다.
- [예제 설정](config/): 뉴스 출처별 설정을 제공합니다.
- [소스 코드](src/news_agent/): 기사 수집, 분석, 저장, 알림과 상태 감시 구현을 담고 있습니다.

운영 문서에 기록된 과거 점검 결과는 개별 실행 환경의 기록입니다. 현재 설치된 서비스나
기사 접근 상태는 자신의 환경에서 확인해야 합니다.
