# Local News Agent — 현재 운영 계약

## 1. 제품 목표

이 프로젝트는 새 기사 후보를 찾고, 실제 렌더링된 본문을 안전하게 확인한 뒤,
한국어 분석과 사건 중복 판정을 거쳐 중요한 최초 사건만 Telegram으로 보내는 로컬 뉴스
비서다. Telegram reaction은 다음 선택에 사용할 사용자 선호 memory로 돌아온다.

핵심 원칙은 다음과 같다.

- 본문 전체를 증명하지 못하면 저장·분석·발송하지 않는다.
- 같은 사건의 후속 기사는 기존 Event에 연결하고 새 알림을 만들지 않는다.
- source별 discovery와 browser 정책은 TOML에 두고 Python에 사이트 값을 숨겨 넣지 않는다.
- 각 CLI는 bounded one-shot으로 끝난다. Python 내부에 무한 watcher를 두지 않는다.
- foreground window나 focus 전환 가능성이 있는 브라우저는 자동 실행하지 않는다.
- 화면 없는 Reuters 자동화는 macOS hidden Chrome instance와 ordinary CDP target의
  background/non-focused 동작을 증명하는 전용 mode로만 허용한다.
- retry는 persistent ledger와 한도를 가져야 하며 같은 실패를 매 snapshot마다 반복하지 않는다.

## 2. 2026-08-24 운영 상태

이 절의 “현재”는 2026-08-24 진단 시점 snapshot이다. 저장소 문서가 실제 macOS service
상태의 source of truth는 아니므로 `launchctl print`와 process 목록으로 다시 확인한다.

- 로드된 자동 job은 `com.inertia.news-agent-feedback` 하나였다.
- CNBC `com.inertia.news-agent`와 Reuters 자동 loop는 중지돼 있었다.
- `news-agent run-once`, Reuters 전용 Chrome, CDP/remote-debugging process는 남아 있지
  않았다.
- feedback은 30초마다 reaction/offset/memory만 갱신하고 종료하는 one-shot이다. 브라우저를
  호출하지 않는다.
- Reuters의 마지막 확인 run은 2026-08-24 16:50 KST 무렵 `completed`와 Telegram
  발송까지 성공했다. 그러나 일반 Chrome이 foreground로 올라오는 UX 문제가 확인됐다.
- CNBC는 같은 PRO/Investing Club 계열 두 기사에서 완전한 본문을 증명하지 못해
  fail-closed했다. 거부는 정상이나 같은 URL을 반복 선택한 운영 정책은 결함이었다.

당시 진단을 바탕으로 foreground Chrome 방식은 폐기하고 저장소의 기본 운영 정책을
다음처럼 재설계했다. 템플릿 존재 여부와 실제 `launchctl` 등록 상태는 구분한다.

| 대상 | 실행 정책 | 브라우저 정책 |
| --- | --- | --- |
| CNBC | 60초 LaunchAgent one-shot 템플릿 | 전용 headless Chrome, GUI fallback 금지 |
| Reuters | 900초 LaunchAgent one-shot 템플릿 | macOS hidden Chrome + ordinary background/non-focused CDP target, fallback 금지 |
| Feedback | 30초 LaunchAgent, idle이면 stdout 억제 | 브라우저를 사용하지 않음 |
| Reuters watchdog | 300초 LaunchAgent, 상태·장애·복구 확인 | 브라우저를 사용하지 않음 |

네 LaunchAgent 템플릿의 설치·검증·중지 절차는 `launchd/README.md`가 담당한다. watchdog는
2026-08-28 추가한 운영 기능이며 위 진단 시점에 실행 중이었다는 뜻이 아니다.

## 3. source별 discovery와 Reader

### CNBC

CNBC는 공식 News Sitemap을 조건부 GET한다. canonical `<loc>`를 `guid`와 `url`로 쓰고,
`news:title`, timezone-aware `news:publication_date`, comma-separated keyword token을
검증한다. 기본 설정은 exact `Earnings` token만 제외한다. substring이나 본문 내용으로
filter하지 않는다.

Sitemap이 `ETag` 또는 `Last-Modified`를 주면 SQLite validator를 다음 `run-once`에서
재사용한다.

- `200`: body를 parse하고 eligible item을 만든 뒤 validator를 checkpoint한다.
- `304`: discovery body를 parse하지 않는다. 그래도 due analysis retry와 notification은
  처리한다.

Reader는 설치된 Google Chrome executable과 격리 profile로 Playwright persistent
context를 headless 실행한다. HTTP 401, challenge, timeout 또는 어떤 실패에서도 일반
Chrome으로 자동 fallback하지 않는다.

CNBC 설정의 단일 `article_access_denied_selector`와
`article_access_denied_phrases`는 “Subscribe to CNBC PRO” 같은 접근 제한을 식별하는 데
사용한다. 정상 본문 추출이 실패한 뒤에만 이 scoped gate와 그 안의 visible descendant
iframe text를 확인하며, phrase가 일치하면 nonretryable `access_restricted`로 분류한다.
H1이 보이더라도 configured article body에서 완전한 문단 column을 증명하지 못하면 원문
HTML, page 전체 `p`, teaser 또는 구독 문구를 본문으로 추정하지 않는다.

### Reuters

Reuters discovery는 설정된 Business, Markets, World, Technology section을 전용 profile의
일반 Google Chrome 엔진으로 읽는다. 다만 `browser_launch_mode = "hidden_cdp"`는 macOS에서
격리 Chrome instance를 hidden·non-activating 상태와 startup window 0개로 시작한다. 그런
다음 ordinary CDP target을 `background=true`, `focus=false`로 한 번 만들어 section과 기사
navigation에 재사용한다. foreground app과 화면상 Chrome window를 바꾸지 않는 것이 이
mode의 운영 계약이지만 Chrome 전체에 대한 절대 보장은 아니다. Chrome 151을 쓰는 이 Mac의
격리 실험에서는 foreground app 유지와 화면상 실험 window 0개를 확인했다. 이 조건이나
cleanup 회귀는 수동 smoke와 운영 관찰로 잡는다. 코드는 hidden launch,
background/non-focused target 생성, 전용 프로세스 cleanup을 증명하지 못하면
읽은 본문을 버리지만, 매 run에서 macOS 화면을 동적으로 계측하지는 않는다.

자동 job과 수동 smoke는 다음과 같이 동일한 bounded command를 사용한다.

```sh
/Users/YOUR_USER/Desktop/news_agent/.venv/bin/news-agent run-once \
  --config /Users/YOUR_USER/Desktop/news_agent/config/reuters.toml
```

이 경로는 macOS AppKit/Launch Services와 설치된 Chrome에서 ordinary CDP target을
background/non-focused로 생성하는 동작에 의존하므로 로컬 macOS 전용이다. OS hidden
launch나 target 생성이 실패해도 headless 또는 visible `regular_cdp`로 자동 fallback하지
않는다. `regular_cdp`를 명시한 별도 config는 계속 `--allow-visible-browser` opt-in이
필요하며 자동 scheduler에 연결하지 않는다.

### 공통 Reader gate

본문은 다음 조건을 모두 통과한 뒤에만 저장한다.

1. 입력과 final URL이 허용된 HTTPS article URL이다.
2. navigation response가 성공이고 challenge/landing page로 바뀌지 않았다.
3. discovery 제목과 대응하는 유일한 rendered H1이 있다.
4. 허용되지 않은 JavaScript/rendered dialog가 없다.
5. source-configured selector 안에 visible하고 연속적인 유일한 우세 문단 column이 있다.
6. page, context, driver cleanup과 profile lock release가 완료된다.

cleanup을 증명하지 못하면 이미 추출한 body도 버린다. personal Chrome profile, 다른 Reader
backend 또는 CUA로 몰래 우회하지 않는다. legacy `cua` mode는 명시적으로 선택한 별도
config의 회귀 호환 경로일 뿐 production fallback이 아니다.

## 4. 저장 모델과 durable ledger

### 기사와 Event

`articles`는 source identity, canonical URL, 원문 제목·본문, 한국어 번역 제목·요약을
보관한다. 본문 저장과 분석은 구분된다. Reader가 성공한 뒤 분석이 실패해도 검증된 article
row는 유지된다.

분석은 원문 제목과 lead로 FTS5 Event 후보를 최대 2개 찾고, 한 번의 구조화된 Codex 호출로
번역 제목, 상세 요약과 다음 판정 중 하나를 만든다.

- `new_event`: 새 Event와 notification outbox snapshot을 같은 transaction으로 생성한다.
- `existing_event`: 전달된 후보 Event 중 하나에 연결하고 새 알림은 만들지 않는다.
- `non_event`: 기사 분석은 저장하되 Event와 알림은 만들지 않는다.

후보에 없던 Event ID, decision/ID 불일치, partial analysis pair는 저장하지 않는다.

### 기사 read 실패

선택된 기사를 읽지 못하면 `(source_id, url)` primary key의 `article_read_failures`에 다음
운영 정보를 남긴다.

- discovery `guid`, `title`
- `status`: `retry_wait` 또는 `dead`
- bounded `reason`과 `last_error`
- `attempts`, `first_failed_at`, `last_failed_at`, `next_attempt_at`

기본 policy field는 다음과 같다.

```toml
article_read_retry_max_attempts = 3
article_read_retry_base_seconds = 900
article_read_retry_max_seconds = 21600
```

retryable 실패의 첫 delay는 900초이며 이후 두 배로 늘되 21,600초를 넘지 않는다.
최대 시도에 도달하거나 접근 제한처럼 재시도해도 해결되지 않는 실패는 `dead`가 되고
`next_attempt_at`을 두지 않는다.

fresh run selection과 browser candidate prompt는 ledger에 남아 있는 URL을 due 여부와
관계없이 제외한다. `discover`의 pending 조회는 due 상태를 다시 보여줄 수 있지만 실제
자동 재시도는 별도 queue만 담당한다.

- `retry_wait`이고 `now < next_attempt_at`: queue 대기
- `retry_wait`이고 due: oldest-first read-retry queue에서 eligible
- `dead`: 계속 제외하며 자동 queue에 넣지 않음
- 같은 source에서 GUID 또는 URL이 일치하는 article 저장 성공: matching failure row 제거

이 계약으로 Sitemap의 modified response나 Reuters candidate 수집이 같은 inaccessible URL을
무한 반복하지 않는다. due read retry는 fresh selection과 분리된 oldest-first queue에서 run당
최대 1건을 처리하므로 fresh `run_cap` skip 대상이 아니며 Sitemap `304`에서도 진행된다.
ledger에는 기사 body, summary, Telegram token이나 browser profile 내용을 기록하지 않는다.

### 분석 retry

Reader 성공 후 분석만 실패한 저장 기사는 `article_analysis_retries`에 등록한다. retry는
Reader를 다시 실행하지 않고 저장된 body를 사용한다.

각 `run-once`는 fresh 기사 최대 3건과 별도로 due analysis retry 최대 1건을 선택한다.
fresh가 있어도 retry를 굶기지 않고 Sitemap `304`에서도 실행한다. 성공하면 retry row를
지운다. retry 후보는 가장 오래 전에 실패한 순서로 골라 한 실패가 나머지 backlog를
가로막지 않게 한다. migration 전부터 존재했지만 ledger에 없는 legacy unresolved article을 임의로
backfill하지 않는다.

### 알림 outbox

`new_event`만 channel-neutral `notification_outbox`를 만들고 configured target별 delivery를
생성한다. Telegram sender는 번역 제목, 상세 요약, 원문 URL을 4,096자 한도에 맞춰
손실 없이 나눈다.

delivery 상태는 `pending`, `sending`, `retry_wait`, `sent`, `dead`이며 claim token과 lease로
중복 worker를 막는다. retryable network/429/5xx는 capped exponential backoff와 유효한
Telegram `retry_after`를 반영한다. terminal 4xx, 최대 시도 또는 부분 발송은 `dead`다.

bot token은 TOML, plist, DB 또는 로그에 넣지 않는다. 환경변수 또는 macOS Keychain
service/account reference로만 조회한다.

### reaction memory

`telegram-feedback-once`는 Telegram update offset과 message reaction을 SQLite에
idempotent하게 기록하고 shared `data/memory/MEMORY.md`를 갱신한다. memory 파일은 완성된
내용을 원자적으로 교체해 중간 파일이 보이지 않게 한다. selection prompt에는 길이 제한을
적용한 memory context만 전달한다.

`--quiet-when-idle`은 target이 없거나 새 update·reaction이 없고 memory가 바뀌지 않은
정상 tick의 최종 stdout payload를 억제한다. 오류를 숨기거나 exit code를 바꾸지 않는다.

## 5. `run-once` 순서와 bounded work

```text
TOML load
    ↓
<database>.run-once.lock nonblocking 획득
    ├─ contention → already_running
    └─ 획득 → 아래 workflow 동안 보유
    ↓
browser launch-mode permission gate + source discovery
    ├─ CNBC Sitemap 200 → parse + validator checkpoint 대상
    ├─ CNBC Sitemap 304 → fresh item 없음
    └─ Reuters browser discovery → hidden-CDP background target에서 실행
    ↓
stored article + run_cap skip + article_read_failures 상태로 후보 제거
    ↓
fresh 최대 3건 선택
    └─ 상한 밖 fresh → discovery_skips(reason=run_cap)
    ↓
각 fresh article: Reader → store → analyze/Event
    └─ read 실패 → cooldown/dead ledger 갱신, 다음 item 계속
    ↓
due article read retry 최대 1건을 별도 처리
    └─ fresh 유무와 Sitemap 304 여부에 관계없이 실행, run_cap 제외
    ↓
analysis retry 최대 1건을 별도로 처리
    └─ fresh 유무와 Sitemap 304 여부에 관계없이 실행
    ↓
due notification 최대 3회 drain
    ↓
구조화 결과 출력 + lock release
```

한 run의 config와 discovery snapshot은 재사용한다. `run_cap`은 한 snapshot의 과도한 backlog를
영구 제외하기 위한 별도 정책이고, 일시적인 read failure cooldown과 혼동하지 않는다.

개별 article read/analysis 실패는 다음 item과 notification drain을 막지 않되 최종 status를
`completed_with_errors`로 만든다. configuration, discovery setup 또는 lock 자체 실패는
terminal `failed`다. `failed`와 `completed_with_errors`는 CLI exit 1이며,
`completed`, `no_work`, `already_running`은 exit 0이다.

## 6. CLI

모든 command는 한 번 수행하고 종료한다.

```sh
# 저장하지 않고 pending discovery 확인
.venv/bin/news-agent discover --config config/cnbc-business.toml

# exact discovery identity 한 건 읽고 저장
.venv/bin/news-agent read-one --config config/cnbc-business.toml --guid <IDENTITY>

# 저장된 기사 한 건 분석·Event 판정
.venv/bin/news-agent summarize-one --config config/cnbc-business.toml --guid <GUID>

# due notification 최대 한 건 발송
.venv/bin/news-agent notify-one --config config/cnbc-business.toml

# feedback 한 번 확인; 정상 idle stdout 억제
.venv/bin/news-agent telegram-feedback-once \
  --config config/reuters.toml --quiet-when-idle

# CNBC bounded workflow
.venv/bin/news-agent run-once --config config/cnbc-business.toml

# Reuters hidden-CDP bounded workflow
.venv/bin/news-agent run-once --config config/reuters.toml

# Reuters 감시 결과만 확인: Telegram 발송·state file 갱신 없음
.venv/bin/news-agent watchdog-once \
  --config config/reuters.toml \
  --service-label com.inertia.news-agent-reuters \
  --run-log /Users/YOUR_USER/Library/Logs/news-agent.reuters.run-once.stderr.log \
  --state-file /Users/YOUR_USER/Desktop/news_agent/data/reuters-watchdog.json \
  --dry-run
```

`discover`와 `read-one`도 `hidden_cdp` config에서는 화면 승인 flag 없이 실행한다.
`regular_cdp`처럼 실제 foreground window를 만들 수 있는 별도 config만
`--allow-visible-browser` opt-in이 필요하다.

## 7. 설정 경계

다음 값은 source 또는 환경에 따라 달라지므로 TOML에 둔다.

- source ID, Sitemap/RSS/browser section URL과 keyword filter
- HTTP user agent와 timeout
- database와 memory 경로
- browser mode, launch mode, executable, 격리 profile
- allowed host/path와 candidate/read limit
- article body selector와 단일 access-denied gate selector/phrases
- read retry max attempts/base/max seconds
- Codex timeout과 memory context limit
- notification lease, retry policy, target metadata와 secret source reference

표준 field 이름, 안전 상태 이름, bounded work limit처럼 알고리즘 계약 자체인 값은 코드에
둘 수 있다. secret, 개인 Chrome directory, filesystem root와 home directory는 browser
profile 설정으로 받지 않는다.

## 8. 실패 처리 원칙

- discovery payload가 invalid하면 빈 성공 결과로 바꾸지 않고 run을 실패시킨다.
- URL/H1/body/dialog/cleanup gate 중 하나라도 실패하면 article을 저장하지 않는다.
- 접근 제한으로 감지된 CNBC 기사는 terminal read failure로 기록해 자동 재시도하지 않는다.
- 일시적인 navigation/browser 실패만 bounded cooldown 후 다시 시도한다.
- 분석 실패는 검증된 article을 삭제하지 않고 별도 analysis ledger에 남긴다.
- 알림 실패는 Event/analysis transaction을 롤백하지 않고 delivery 상태로 남긴다.
- 로그에는 body, summary, token, Keychain value, browser profile 내용을 넣지 않는다.
- foreground Chrome을 띄울 수 있는 작업은 flag 없이 시작하지 않는다.
- Reuters hidden-CDP가 실패하면 headless나 visible browser로 fallback하지 않는다.

## 9. 자동 운영

자동 실행 템플릿은 CNBC, Reuters, feedback과 Reuters watchdog 네 개다.

- `launchd/com.inertia.news-agent.plist`: CNBC 60초 one-shot 템플릿
- `launchd/com.inertia.news-agent-reuters.plist`: Reuters 900초 one-shot 템플릿
- `launchd/com.inertia.news-agent-feedback.plist`: feedback 30초 quiet-idle 템플릿
- `launchd/com.inertia.news-agent-watchdog.plist`: Reuters 감시 300초 quiet-idle 템플릿
- `launchd/news-agent.newsyslog.conf`: 여덟 stdout/stderr 로그를 1 MiB, archive 7개로 제한

Reuters는 화면 안전성 검증을 우선하기 위해 900초라는 보수적 cadence를 사용한다. 저장소에
plist가 존재한다는 사실은 현재 service가 등록됐다는 뜻이 아니다. 설치, 현재 상태 확인,
rollback 절차는 `launchd/README.md`를 따른다.

### Reuters 장애·복구 알림

watchdog는 Reuters·feedback과 분리된 browser-free one-shot으로 service 등록·disabled
상태와 Reuters 구조화 run 로그를 확인한다. service가 누락/disabled이거나, 연속 2회 이상
`failed`/`completed_with_errors`가 발생하거나, 45분 넘게 진행이 없거나, 실행 중인 run이
30분 넘게 끝나지 않으면 Telegram 장애 알림 대상으로 삼는다. sleep/관찰 공백 이후에는
오래된 로그·이력 기반 판단에 20분 유예를 둔다.

`data/reuters-watchdog.json`에 대상별 incident와 알림 재시도 상태를 보존한다. Telegram
전송 성공을 확인하고 그 결과를 저장한 뒤 같은 장애의 중복 발송을 억제하지만, Telegram이
수락한 뒤 process 종료나 network timeout으로 receipt를 기록하지 못하면 재시도 때 중복될
수 있다. Telegram idempotency key가 없어 exactly-once 발송은 보장하지 않는다. 전송 재시도
간격은 5분, 10분처럼 두 배로 늘리되 최대 1시간이며, 영구 거부도 1시간보다 자주
재시도하지 않는다.

복구는 장애 감지 시점 이후 새로 완료된 `completed`/`no_work` 성공을 확인한 뒤 알린다.
과거에 저장된 성공이나 `running`/`already_running`만으로 해제하지 않는다. Reuters
config의 Telegram target과 Keychain reference를 재사용하되 기사 DB·outbox나 feedback
offset을 변경하지 않는다.

watchdog는 401을 해결하거나 Reuters를 자동 재시작하지 않는다. Mac이 꺼짐/sleep 상태거나
네트워크가 끊긴 동안은 즉시 Telegram을 보낼 수 없다. 의도적으로 Reuters를 중지하면서
알림을 원하지 않으면 watchdog도 함께 중지한다. `--dry-run`은 읽기 전용이며 발송과
state file 갱신을 하지 않는다.
watchdog는 자신이 아니라 Reuters를 감시하며 같은 로컬 Python 설치와 Reuters config·Telegram
인증 정보를 공유한다. watchdog 자체나 이 공용 의존성의 장애는 자기 알림을 보장할 수
없으며, 이를 감지하려면 호스트와 독립된 외부 감시가 필요하다.

## 10. 검증 기준

2026-08-28 사용자 요청으로 전체 자동 테스트 모음과 pytest 개발 의존성을 제거했다.
변경 뒤에는 다음 정적 검사와 필요한 범위의 수동 검증을 수행한다. 정적 검사는 삭제된
자동 테스트의 동작 검증을 대체하지 않는다.

```sh
.venv/bin/python -m compileall -q src/news_agent
.venv/bin/news-agent --help
uv lock --check
plutil -lint launchd/com.inertia.news-agent.plist
plutil -lint launchd/com.inertia.news-agent-reuters.plist
plutil -lint launchd/com.inertia.news-agent-feedback.plist
plutil -lint launchd/com.inertia.news-agent-watchdog.plist
sudo newsyslog -nv -f launchd/news-agent.newsyslog.conf
```

브라우저 smoke, production DB migration, Codex 호출 또는 Telegram 발송을 실행하지 않았다면
정적 검사 통과만으로 그것들을 검증했다고 쓰지 않는다. 실제 운영 상태도 별도 read-only
명령과 watchdog의 `--dry-run`으로 확인한다.

## 11. 보존할 과거 검증 이력

다음은 설계 근거로 유용하지만 현재 활성 상태를 뜻하지 않는 과거 snapshot이다.

- 2026-08-11~12에 CNBC Sitemap conditional GET/`304`, isolated headless Chrome Reader,
  SQLite migration/integrity, Codex Event 판정과 Telegram outbox delivery를 단계별로
  production 또는 격리 smoke했다.
- CNBC Reader는 일반 article body와 live-update의 고정 소개 본문만 source selector로
  허용하고 nested 추천·newsletter·footer 문단을 제외하도록 좁혔다.
- Telegram 직접 smoke와 production delivery receipt는 API/DB 성공 증거이며 사용자의 화면
  확인을 자동으로 의미하지 않는다.
- 2026-08-24 Reuters run 성공 뒤 visible Chrome foreground 문제가 확인되어 기존 120초
  scheduler를 제거했다. 이후 설계는 창을 띄운 뒤 최소화하지 않고 startup window가 없는
  macOS hidden·non-activating Chrome instance를 시작한 뒤 ordinary CDP target을
  background/non-focused로 생성해 재사용하며 초기 cadence를 900초로 낮춘다.
- 같은 날 CNBC PRO 계열 read failure 반복을 확인해 `article_read_failures` cooldown/dead
  ledger와 analysis retry starvation 방지 정책을 도입했다.

과거의 article/Event/outbox row 수, Chrome version, receipt ID와 분 단위 cadence 결과는 당시
관측치다. 현재 DB 상태나 미래 정상 동작을 보장하는 운영 지표로 사용하지 않는다.

## 12. 다음 확장 원칙

CNBC와 Reuters의 이 계약이 안정된 뒤에만 다른 section/source, 기존 Event의 리서치 노트
보강, Hermes adapter, Event 병합·분할, Skill/MCP/Plugin 배포를 각각 작은 Increment로
추가한다. 새 source를 이유로 공통 추상화나 foreground GUI 제어를 미리 만들지 않는다.
