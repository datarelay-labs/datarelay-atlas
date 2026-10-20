# Atlas 사용자 운영 콘솔 실행 파일

## 제공 범위와 현재 상태

`atlas_owner_console.py`는 기존 검증된 오프라인 Atlas 배포 패키지를 사용자 소유의 승인된 운영 콘솔에서 실행하는 진입점입니다. 배포 트랜잭션을 새로 구현하지 않고 기존 `deploy.py`와 `operator.py`의 백업·검증·복구 로직을 그대로 사용합니다.

**파일 생성·테스트와 운영 전환 승인은 별개입니다.** 현재 포함된 r3는 frozen application `826c26604c4b47b6aab45a889a15b9552985b5ef`의 439개 파일입니다. 최신 개발 PR의 전체 440개 파일 또는 아직 확정되지 않은 최종 GitHub main을 대신하는 새 릴리스가 아닙니다. 기존 r2/r3 및 운영 Stage를 수정하거나 재명명하지 않습니다.

현재 main 불일치, 미설정 보호 규칙, Incident #356의 미해제 freeze가 남아 있으므로 `prepare`, `preflight`, `deploy`는 실행 파일 내부에서도 중단됩니다. 차단을 건너뛰는 `--force`, `--skip` 옵션은 없습니다. 새 버전은 정식 병합·검증 후 생성한 정확한 main 패키지와 그때 검증된 SHA-256을 `--archive`와 `--sha256`으로 지정해 사용할 수 있습니다. 체크섬 입력은 코드의 무결성 확인이지 릴리스 승인 생성이 아닙니다.

## 명령

| 명령 | 실제 동작 |
| --- | --- |
| `inspect` 또는 명령 생략 | 압축 파일 전체 SHA-256, 내부 37개 파일·29개 wheel·manifest를 검사. 압축 해제/서비스 변경 없음. |
| `unpack` | 승인된 prod-atlas root 콘솔에서 검증된 파일을 새 root 전용 디렉터리에 해제. 서비스·운영 데이터 변경 없음. 동일 파일은 재사용하지만 변조·불완전 디렉터리는 덮어쓰지 않음. |
| `release-check` | GitHub의 현재 main HEAD, 실제 main 보호 규칙, GitHub Actions 발행 체크 정책, Incident #356 상태를 HTTPS GET으로 확인. 상태 변경 없음. |
| `prepare` | 기술적 릴리스 전제조건이 충족된 경우에만 기존 배포기로 별도 소스·venv·Stage 준비. 운영 코드 교체 없음. |
| `preflight` | 성공한 prepare 기록이 필요. 백업 복원/호환성, 기존 서비스 계약, DynamicUser, 기존 공용 HTTPS/Web을 기존 배포기로 점검. |
| `deploy` | 성공한 preflight 기록, 릴리스 전제조건, 실제 대화형 TTY가 필요. 기존 배포기의 정확한 확인 문구를 입력한 뒤에만 운영 전환을 실행. |

`release-check`가 통과해도 공식 Surface Reconciliation / Full User E2E를 수행하거나 인증한 것이 아닙니다. 정식 사용자 게이트와 운영 승인·소유자 수락은 별도 증거로 확인해야 합니다. 이 프로그램은 `USER_E2E_AND_RELEASE_AUTHORITY=NOT_ATTESTED_BY_THIS_PROGRAM`을 출력합니다.

## 파일 전달

배포 제어 패키지는 개발 서버의 `/home/aella/datarelay-atlas-release-artifacts/atlas-owner-console-kit-20261020.tar.gz`에 생성합니다. 내부에는 실행 Python 파일, 기존 r3 오프라인 패키지, 안내문, SHA256SUMS만 포함합니다. 비밀번호, 토큰, TLS 개인키, 서비스 환경파일은 포함하지 않습니다.

기존 승인된 파일 전송 경로를 이용해 dev-atlas에서 실행합니다. 서버 재시작이나 시스템 권한 변경을 포함하지 않습니다.

```sh
scp /home/aella/datarelay-atlas-release-artifacts/atlas-owner-console-kit-20261020.tar.gz prod-atlas:/home/aella/
```

## 지금 실행할 수 있는 검사

1. prod-atlas의 기존 root 콘솔에서 전달받은 외부 tar.gz의 SHA-256을 안내받은 값과 비교합니다. 다르면 진행하지 않습니다.
2. `/root/atlas-owner-console-kit-20261020`이 이미 있으면 자동 덮어쓰지 말고 기존 파일부터 확인합니다.
3. 검증된 외부 패키지를 root 소유의 새 디렉터리에 해제하고 그 안의 SHA256SUMS를 확인합니다.
4. 그 디렉터리에서 아래를 실행합니다. `-I`는 사용자 Python 경로·환경을 격리하고 `-B`는 bytecode 파일 생성을 방지합니다.

```sh
python3 -I -B atlas_owner_console.py inspect
python3 -I -B atlas_owner_console.py release-check
```

`inspect`의 정상 결과는 다음과 같습니다.

```text
ARCHIVE_SHA256=a3630b24598f7b05a09b5677d2249e97fd4f844e44d93e66d0677a6afa2a0891
APPLICATION_HEAD=826c26604c4b47b6aab45a889a15b9552985b5ef
BUNDLE_FILES=37
INSPECT_ONLY:NO_EXTRACTION_NO_SERVICE_CHANGE
```

현재 `release-check`에는 `GITHUB_MAIN_HEAD_MISMATCH`, `INCIDENT_356_NOT_RELEASED`, `MAIN_PROTECTION_INCOMPLETE` 등의 차단 원인이 나올 수 있습니다. GitHub API가 네트워크/제한 때문에 읽히지 않으면 `GITHUB_RELEASE_FACTS_UNAVAILABLE`로 중단하며 성공을 추정하지 않습니다. 이때 서비스는 변경되지 않습니다.

## 정식 전환 조건이 충족된 뒤의 실행 순서

**아래는 현재 r3를 지금 설치하라는 지시가 아닙니다.** 최종 main에 맞는 패키지, 공식 사용자 검증 및 릴리스 승인, Incident 해제가 확인된 다음 해당 패키지의 경로/체크섬을 지정하고 순서대로 실행합니다.

```sh
python3 -I -B atlas_owner_console.py unpack --archive "$APPROVED_ARCHIVE" --sha256 "$VERIFIED_SHA256"
python3 -I -B atlas_owner_console.py prepare --archive "$APPROVED_ARCHIVE" --sha256 "$VERIFIED_SHA256"
python3 -I -B atlas_owner_console.py preflight --archive "$APPROVED_ARCHIVE" --sha256 "$VERIFIED_SHA256"
python3 -I -B atlas_owner_console.py deploy --archive "$APPROVED_ARCHIVE" --sha256 "$VERIFIED_SHA256"
```

한 단계라도 오류면 다음 명령을 실행하지 않습니다. 오류 출력과 마지막 성공 단계를 그대로 보존합니다. 내부 `deploy.py`를 직접 호출해 외부 진입점의 차단을 건너뛰지 않습니다.

## 보존과 장애 처리

실행 파일은 `/run/atlas-owner-console.lock`을 이용해 동시에 두 번 진행하지 못하도록 합니다. 해제 디렉터리는 `/root/atlas-owner-console-<외부 패키지 SHA 앞 16자리>`이고 단계 상태 파일은 그 옆에 둡니다. 부분 해제나 변조 파일을 자동 삭제·복구하지 않습니다.

실패/중단한 전환이 이전 preflight 성공 기록을 재사용하지 않도록 전환 시작 시 기록을 먼저 무효화합니다. 이후 기존 배포기의 오류 코드를 그대로 실패로 처리합니다. 현재 운영 데이터·이전 r2/r3 Stage·롤백 백업을 삭제하지 않습니다.

승인된 실제 deploy에서 실패가 감지되면 기존 배포기가 코드/환경/Ingress 복구를 시도합니다. 복구 실패는 성공으로 보고하지 않습니다. 전원 차단·SIGKILL을 포함한 모든 상황의 자동 복구를 보장하는 프로그램이 아닙니다. 실제 운영 전환 후 ChatGPT native MCP 메모리/소스 쓰기·재시도·읽기 검증은 ChatGPT가 직접 수행합니다.

Desktop Commander 등의 특권 명령 거부를 다른 연결·사용자·명령 형태로 우회하기 위한 파일이 아닙니다. 승인된 사용자 운영 관리 세션에서만 실행합니다.

## 개발 검증

```sh
python3 -m unittest tests.test_atlas_owner_console -v
python3 scripts/atlas_owner_console.py inspect --archive /home/aella/datarelay-atlas-release-artifacts/atlas-owner-deploy-826c266-r3-20261019.tar.gz
```

경로 이탈, 링크·특수 파일, 중복 파일, archive 및 내부 해시 변조, 잘못된 사용자·호스트, 불완전 해제, 잠금 충돌, 순서 위반, 실패 후 상태 보존, 릴리스 차단 전 하위 프로세스 미호출을 격리된 임시 파일시스템에서 검증합니다. 이것은 배포 도구 회귀 검증이며 운영/사용자 게이트 PASS가 아닙니다.
