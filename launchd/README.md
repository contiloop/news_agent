# macOS LaunchAgent 운영

이 디렉터리는 로그인한 사용자 GUI session에서 실행할 네 개의 LaunchAgent 템플릿과
로그 회전 템플릿을 보관한다. Reuters도 자동 실행할 수 있지만 화면을 활성화하지 않는
`hidden_cdp` 브라우저 계약을 먼저 충족해야 한다.

저장소 템플릿을 수정해도 이미 설치된 `~/Library/LaunchAgents` 파일이나 현재
`launchctl` 상태는 자동으로 바뀌지 않는다. 아래 표는 저장소 템플릿의 운영 의도이며
현재 설치 상태가 아니다. 작업 전후에는 반드시 `launchctl print`로 다시 확인한다.

## 운영 의도

| Label | 역할 | 템플릿 cadence |
| --- | --- | ---: |
| `com.inertia.news-agent` | CNBC `run-once` | 60초 |
| `com.inertia.news-agent-reuters` | Reuters `run-once` | 900초 |
| `com.inertia.news-agent-feedback` | Telegram reaction 수집과 memory 갱신 | 30초 |
| `com.inertia.news-agent-watchdog` | Reuters 상태 확인과 장애·복구 Telegram 알림 | 300초 |

저장소의 feedback 템플릿은 `telegram-feedback-once --quiet-when-idle`만 실행한다. update가 없을 때
성공 로그를 매 tick마다 쓰지 않으며, reaction offset과 관찰 결과를 CNBC·Reuters가
공유하는 SQLite와 `data/memory/MEMORY.md`에 반영한다. 기사 discovery나 Reader를
호출하지 않으므로 Chrome을 열지 않는다.

어떤 plist도 저장소에 존재한다는 사실만으로 현재 활성 상태를 뜻하지 않는다. Reuters
템플릿은 정적 검사와 hidden-CDP 수동 검증이 성공한 뒤에만 설치한다.

## Reuters hidden-CDP 계약

Reuters는 headless Chromium이 아니라 설치된 일반 Google Chrome 엔진과 격리된 전용
profile을 사용한다. macOS에서 새 Chrome instance를 hidden·non-activating 상태와 startup
window 0개로 시작한 뒤, ordinary CDP target을 `background=true`, `focus=false`로 한 번
만들어 section과 기사 navigation에 재사용한다. foreground app과 화면상 Chrome window를
바꾸지 않는 것이 이 mode의 운영 계약이지만 Chrome 전체에 대한 절대 보장은 아니다.
Chrome 151을 쓰는 이 Mac의 격리 실험에서는 foreground app이 유지되고 화면상 실험 window가
0개인 것을 확인했다. 코드는 hidden launch, background target 생성, 전용
프로세스 cleanup에 실패하면 run을 fail-closed하며, foreground/window 회귀는
매 run의 동적 화면 계측이 아니라 smoke test와 운영 관찰로 확인한다.

자동 job과 수동 smoke는 같은 명령을 사용하며 visible-browser 승인 flag를 넣지 않는다.

```sh
/Users/YOUR_USER/Desktop/news_agent/.venv/bin/news-agent run-once \
  --config /Users/YOUR_USER/Desktop/news_agent/config/reuters.toml
```

`hidden_cdp`가 실패했다고 headless나 visible `regular_cdp`로 자동 fallback하지 않는다.
macOS AppKit/Launch Services와 Google Chrome CDP 동작에 의존하는 로컬 macOS 전용
경로이며 다른 OS에서는 명시적으로 지원하지 않는다. Reuters와 CNBC는 같은 DB 및
`<database>.run-once.lock`을 공유하므로 겹친 invocation은 `already_running`으로 끝난다.
Reuters의 900초 cadence는 화면 안전성과 기사 접근을 먼저 관찰하기 위한 보수적인
초기값이다.

## Reuters watchdog 계약

`com.inertia.news-agent-watchdog`는 Reuters와 feedback 어느 쪽에도 종속되지 않는 별도
one-shot이다. 브라우저나 Codex 분석을 실행하지 않고 `launchctl` 상태와 Reuters의 구조화
stderr run 로그를 확인한다. Telegram 대상과 Keychain reference는 Reuters config를
사용하고, 관찰·incident·발송 재시도 상태는 `data/reuters-watchdog.json`에 보관한다.

- Reuters service가 등록되지 않았거나 disabled인 상태를 감지한다.
- `failed` 또는 `completed_with_errors`가 2회 이상 연속되면 알린다.
- 45분 넘게 진행이 없거나, 실행 중인 run이 30분 넘게 끝나지 않으면 알린다.
- Mac sleep 등으로 watchdog 관찰에 공백이 생긴 뒤에는 20분의 유예를 두어 오래된
  로그·이력만으로 장애를 오판하지 않는다. service 누락·disabled 확인은 별도다.
- 대상별 같은 incident는 Telegram 전송 성공을 확인하고 그 결과를 디스크에 보존한 뒤
  중복 알림을 억제한다. Telegram이 수락했지만 process 종료나 network timeout으로
  receipt를 기록하지 못하면 재시도 때 중복될 수 있다. Telegram idempotency key가 없으므로
  exactly-once 발송을 보장하지 않는다.
- 알림 전송 재시도는 5분, 10분처럼 두 배로 늘리되 최대 1시간 간격으로 제한한다.
  영구 거부로 분류된 전송도 1시간보다 자주 재시도하지 않는다.
- 복구는 장애 감지 시점 이후 새로 완료된 `completed` 또는 `no_work` 성공을 확인한 뒤
  알린다. 과거에 저장된 성공, 단순 재시작, `running`, lock 경합의 `already_running`은
  복구 증거가 아니다.

이 watchdog는 Reuters의 401을 해결하거나 job을 자동 재시작하지 않는다. Mac 자체가
꺼지거나 잠들면 확인할 수 없고, 네트워크나 Telegram이 끊기면 그동안 알림을 전달할 수
없다. 같은 Mac에서 실행되는 로컬 감시이므로 호스트 장애까지 실시간으로 보장하지 않는다.
watchdog 자체가 아니라 Reuters를 감시하며, 같은 로컬 Python 설치와 Reuters config·Telegram
인증 정보를 공유한다. watchdog나 이 공용 의존성이 고장 나면 자기 장애 알림을 보장할 수
없다. 이 범위까지 감시하려면 해당 호스트와 독립된 외부 감시가 필요하다.

상태를 변경하거나 Telegram을 보내지 않는 점검은 다음과 같다. `--dry-run`은 state file도
갱신하지 않는다.

```sh
/Users/YOUR_USER/Desktop/news_agent/.venv/bin/news-agent watchdog-once \
  --config /Users/YOUR_USER/Desktop/news_agent/config/reuters.toml \
  --service-label com.inertia.news-agent-reuters \
  --run-log /Users/YOUR_USER/Library/Logs/news-agent.reuters.run-once.stderr.log \
  --state-file /Users/YOUR_USER/Desktop/news_agent/data/reuters-watchdog.json \
  --dry-run
```

정기 job은 `--quiet-when-idle`로 정상 idle stdout을 억제한다. 기존 기사 outbox나 feedback
offset은 watchdog 상태 파일로 대체하거나 공유하지 않는다.

## CNBC 실패와 retry 계약

Reader는 CNBC PRO/Investing Club처럼 H1만 보이고 완전한 본문이 증명되지 않는 페이지를
저장하지 않는다. 이 fail-closed 동작은 유지하되 같은 URL을 무한 재시도하지 않는다.

- read 실패는 `(source_id, url)` 기준 `article_read_failures`에 기록한다.
- retryable 실패는 `retry_wait`, terminal 실패나 최대 시도 도달은 `dead`다.
- 기본 정책은 `article_read_retry_max_attempts = 3`,
  `article_read_retry_base_seconds = 900`,
  `article_read_retry_max_seconds = 21600`이다.
- retry delay는 첫 실패 15분에서 시작해 두 배로 늘며 최대 6시간을 넘지 않는다.
- ledger에 남아 있는 URL은 fresh selection과 browser prompt에서 제외한다. due가 된
  `retry_wait`은 별도 oldest-first queue에서 run당 최대 1건만 재시도하므로 fresh
  `run_cap` 대상이 아니며 Sitemap `304 Not Modified`에서도 진행된다.
- 같은 source에서 GUID 또는 URL이 일치하는 기사가 성공적으로 저장되면 실패 ledger를
  지운다.

analysis retry는 fresh 기사에 의해 계속 굶지 않는다. 각 `run-once`는 fresh 기사 최대
3건과 별도로 due analysis retry 최대 1건을 처리하며, Sitemap `304 Not Modified`에서도
due analysis retry와 notification drain을 진행한다. retry는 가장 오래 전에 실패한 항목부터
골라 같은 최신 실패가 queue를 독점하지 않게 한다.

## 실행 의미

- `StartInterval`은 시작 기회를 주는 cadence이지 정확한 실시간 시계나 SLA가 아니다.
- Mac sleep 중이거나 이전 invocation이 계속 실행 중이면 tick이 누락될 수 있고 나중에
  횟수만큼 재생되지 않는다.
- 겹친 `run-once`는 DB sibling nonblocking lock으로 `already_running` 종료한다.
- `RunAtLoad`는 `false`다. 등록 직후 즉시 실행하려면 `kickstart`를 따로 사용한다.
- plist에는 Telegram token이나 다른 secret을 넣지 않는다. 실행 사용자 Keychain에서
  설정의 service/account로 token을 읽는다.
- CNBC의 `dedicated_chrome` headless 실행은 어떤 HTTP status에서도 visible Chrome으로
  자동 fallback하지 않는다.
- Reuters의 `hidden_cdp` 실행도 어떤 실패에서도 headless나 visible Chrome으로 자동
  fallback하지 않는다.

## 전제조건

- 저장소와 가상환경 경로가 `/Users/YOUR_USER/Desktop/news_agent`여야 한다.
- `.venv/bin/news-agent`가 실행 가능하고, 설치된 Google Chrome 및 Playwright Python을
  사용할 수 있어야 한다.
- Reuters `hidden_cdp`는 macOS의 `open`/Launch Services와 ordinary Chrome CDP target의
  background/non-focused 생성에 의존하므로 로그인한 macOS GUI session에서만 지원한다.
- 실행 사용자가 `data/news.sqlite3`, browser profile, 각 sibling lock과
  `~/Library/Logs`에 접근할 수 있어야 한다.
- 비대화형 child process에서도 `codex` 인증이 동작해야 한다. plist `PATH`는 현재
  `/opt/homebrew/bin`을 포함한다.
- 같은 사용자 session에서 Telegram Keychain item을 prompt 없이 읽을 수 있어야 한다.

## 저장소 템플릿 검증

2026-08-28 사용자 요청으로 자동 테스트 모음과 pytest 개발 의존성을 제거했다. 아래
정적 검사는 문법·설정 확인용이며, 삭제된 자동 테스트의 동작 검증을 대체하지 않는다.

```sh
plutil -lint /Users/YOUR_USER/Desktop/news_agent/launchd/com.inertia.news-agent.plist
plutil -lint /Users/YOUR_USER/Desktop/news_agent/launchd/com.inertia.news-agent-reuters.plist
plutil -lint /Users/YOUR_USER/Desktop/news_agent/launchd/com.inertia.news-agent-feedback.plist
plutil -lint /Users/YOUR_USER/Desktop/news_agent/launchd/com.inertia.news-agent-watchdog.plist
/Users/YOUR_USER/Desktop/news_agent/.venv/bin/python -m compileall -q \
  /Users/YOUR_USER/Desktop/news_agent/src/news_agent
/Users/YOUR_USER/Desktop/news_agent/.venv/bin/news-agent --help
sudo newsyslog -nv -f \
  /Users/YOUR_USER/Desktop/news_agent/launchd/news-agent.newsyslog.conf
```

이 검증은 Python 문법, CLI 진입점과 템플릿 구조만 확인한다. 현재 service 등록, production DB, 실제 기사 읽기,
Codex 인증 또는 Telegram 수신을 증명하지 않는다.

## 현재 등록 상태 확인

아래 명령은 상태를 바꾸지 않는다. `Could not find service`는 해당 job이 현재 등록되지
않았다는 뜻이다.

```sh
launchctl print gui/$(id -u)/com.inertia.news-agent
launchctl print gui/$(id -u)/com.inertia.news-agent-reuters
launchctl print gui/$(id -u)/com.inertia.news-agent-feedback
launchctl print gui/$(id -u)/com.inertia.news-agent-watchdog
pgrep -af 'news-agent.*run-once|remote-debugging-port|reuters-chrome-profile'
```

각 job의 `state = not running`은 one-shot으로 실행하고 종료하는 tick 사이에는 정상일 수
있다. `last exit code`, 설치된 `ProgramArguments`, 최근 로그를 함께 본다. service 등록과
exit 0만으로 Reuters 기사 수집이 계속 성공했다고 판단하지 않는다.

## 설치 또는 템플릿 갱신

CNBC는 자동 실행을 정말 재개하기로 결정한 경우에만 복사·등록한다.

```sh
mkdir -p /Users/YOUR_USER/Library/LaunchAgents /Users/YOUR_USER/Library/Logs
cp /Users/YOUR_USER/Desktop/news_agent/launchd/com.inertia.news-agent.plist \
  /Users/YOUR_USER/Library/LaunchAgents/com.inertia.news-agent.plist
plutil -lint /Users/YOUR_USER/Library/LaunchAgents/com.inertia.news-agent.plist
launchctl bootstrap gui/$(id -u) \
  /Users/YOUR_USER/Library/LaunchAgents/com.inertia.news-agent.plist
```

Reuters는 정적 검사와 hidden-CDP 수동 검증을 통과한 뒤에만 별도로 복사·등록한다.
이 절의 명령을 문서화하거나 템플릿을 추가한 것 자체는 live activation이 아니다.

```sh
cp /Users/YOUR_USER/Desktop/news_agent/launchd/com.inertia.news-agent-reuters.plist \
  /Users/YOUR_USER/Library/LaunchAgents/com.inertia.news-agent-reuters.plist
plutil -lint \
  /Users/YOUR_USER/Library/LaunchAgents/com.inertia.news-agent-reuters.plist
launchctl bootstrap gui/$(id -u) \
  /Users/YOUR_USER/Library/LaunchAgents/com.inertia.news-agent-reuters.plist
```

feedback의 설치본을 `--quiet-when-idle` 템플릿으로 갱신하려면 먼저 기존 job을 내리고
새 파일을 복사한 뒤 다시 등록한다. 이미 내려가 있으면 `bootout`의 not-found 오류는
상태 확인 후 무시할 수 있다.

```sh
launchctl bootout gui/$(id -u) \
  /Users/YOUR_USER/Library/LaunchAgents/com.inertia.news-agent-feedback.plist
cp /Users/YOUR_USER/Desktop/news_agent/launchd/com.inertia.news-agent-feedback.plist \
  /Users/YOUR_USER/Library/LaunchAgents/com.inertia.news-agent-feedback.plist
plutil -lint \
  /Users/YOUR_USER/Library/LaunchAgents/com.inertia.news-agent-feedback.plist
launchctl bootstrap gui/$(id -u) \
  /Users/YOUR_USER/Library/LaunchAgents/com.inertia.news-agent-feedback.plist
```

watchdog는 정적 검사·CLI 진입점과 위 `--dry-run` 결과를 확인한 뒤 별도로 설치한다. 이미
등록된 템플릿을 갱신할 때는 해당 watchdog job만 먼저 `bootout`한다.

```sh
install -m 600 \
  /Users/YOUR_USER/Desktop/news_agent/launchd/com.inertia.news-agent-watchdog.plist \
  /Users/YOUR_USER/Library/LaunchAgents/com.inertia.news-agent-watchdog.plist
plutil -lint \
  /Users/YOUR_USER/Library/LaunchAgents/com.inertia.news-agent-watchdog.plist
launchctl bootstrap gui/$(id -u) \
  /Users/YOUR_USER/Library/LaunchAgents/com.inertia.news-agent-watchdog.plist
```

등록 후 즉시 one-shot을 요청할 때만 사용한다.

```sh
launchctl kickstart gui/$(id -u)/com.inertia.news-agent
launchctl kickstart gui/$(id -u)/com.inertia.news-agent-reuters
launchctl kickstart gui/$(id -u)/com.inertia.news-agent-feedback
launchctl kickstart gui/$(id -u)/com.inertia.news-agent-watchdog
```

## 로그와 rotation

자동 job의 로그는 다음 여덟 파일뿐이다.

- `/Users/YOUR_USER/Library/Logs/news-agent.run-once.stdout.log`
- `/Users/YOUR_USER/Library/Logs/news-agent.run-once.stderr.log`
- `/Users/YOUR_USER/Library/Logs/news-agent.reuters.run-once.stdout.log`
- `/Users/YOUR_USER/Library/Logs/news-agent.reuters.run-once.stderr.log`
- `/Users/YOUR_USER/Library/Logs/news-agent.feedback.stdout.log`
- `/Users/YOUR_USER/Library/Logs/news-agent.feedback.stderr.log`
- `/Users/YOUR_USER/Library/Logs/news-agent.watchdog.stdout.log`
- `/Users/YOUR_USER/Library/Logs/news-agent.watchdog.stderr.log`

`news-agent.newsyslog.conf`는 각 로그가 1 MiB에 도달하면 회전하고 gzip archive를 7개
보관하며 daemon에 signal을 보내지 않는다. 현재 checkout의 사용자 `inertia:staff`와
경로를 전제로 한다. 다른 계정에서는 owner와 절대 경로를 먼저 바꾼다. 설치에는 관리자
권한이 필요하다.

```sh
sudo cp /Users/YOUR_USER/Desktop/news_agent/launchd/news-agent.newsyslog.conf \
  /etc/newsyslog.d/news-agent.conf
sudo newsyslog -nv -f /etc/newsyslog.d/news-agent.conf
```

`-n` dry-run으로 대상과 판단을 확인한 뒤에만 실제 rotation을 맡긴다. 템플릿을
저장소에 추가한 사실만으로 `/etc/newsyslog.d`에 설치되지는 않는다.

## 중지

Reuters를 의도적으로 중지하면서 장애 알림도 원하지 않으면 watchdog를 먼저 함께 내린다.
watchdog만 내리면 Reuters 수집과 feedback은 계속 동작한다.

```sh
launchctl bootout gui/$(id -u) \
  /Users/YOUR_USER/Library/LaunchAgents/com.inertia.news-agent-watchdog.plist
launchctl bootout gui/$(id -u) \
  /Users/YOUR_USER/Library/LaunchAgents/com.inertia.news-agent.plist
launchctl bootout gui/$(id -u) \
  /Users/YOUR_USER/Library/LaunchAgents/com.inertia.news-agent-reuters.plist
launchctl bootout gui/$(id -u) \
  /Users/YOUR_USER/Library/LaunchAgents/com.inertia.news-agent-feedback.plist
```

이 작업은 scheduler만 중지하며 DB나 설치 plist를 삭제·복원하지 않는다. 등록 파일도
치우려면 service가 내려간 것을 확인한 뒤 해당 설치 파일만 휴지통으로 옮긴다.

## 과거 검증 이력

- 2026-08-11~12에는 CNBC LaunchAgent와 headless Reader의 production cadence,
  conditional Sitemap `304`, DB integrity 및 Telegram delivery를 검증했다. 당시의 row
  수와 receipt는 그 시점 snapshot일 뿐 현재 활성 상태를 뜻하지 않는다.
- 2026-08-24 16:50 KST 무렵 마지막 Reuters run은 `completed`와 Telegram 전송까지
  성공했지만 일반 Chrome의 foreground 전환이 사용자 화면을 방해했다. 이 관측 때문에
  기존 120초 Reuters 자동 plist를 제거했다. 이후 자동화 계약은 창을 나중에 최소화하는
  방식이 아니라 처음부터 startup window가 없는 hidden·non-activating instance를 띄우고
  ordinary CDP target을 background/non-focused로 생성해 재사용하며, 초기 cadence를
  900초로 낮추는 방식으로 재설계했다.
- 같은 날 CNBC는 PRO/Investing Club 계열 두 기사에서 완전한 본문을 증명하지 못해
  fail-closed했다. 본문 거부는 맞았지만 반복 선택은 운영 결함이어서 durable cooldown/dead
  ledger 정책으로 교체했다.
