# Atlas 운영 전환: 사용자 실행용 오프라인 배포 패키지

**범위:** prod-atlas에서 사전 점검 → 준비 → 최종 승인 → 운영 코드 전환.
이 문서는 사용자 소유의 **정식으로 승인된 운영 관리 세션**에 대한 안내입니다.
기존 Desktop Commander의 차단 명령을 우회하거나 그 설정을 바꾸라는 지시가 아닙니다.
현재 승인된 실행 경계가 별도로 없다면 prepare/preflight/deploy를 실행하지 마세요.



## 추가 개인 참고자료 표시 결함 수정

ChatGPT가 **실제 Playwright Chromium 156**을 조작하며 개인 로컬
Markdown 소스 상세 화면의 출처 표시를 검증한 결과,
`personal reference / non-authoritative` 라벨과 동시에 존재하지 않는
`https://github.com/local/markdown/...`를 `canonical source` 링크로
보여주는 결함을 재현했습니다. 새 불변 애플리케이션 `826c266`는
이 잘못된 링크와 권위 문구를 수정했습니다.
같은 테스트용 실제 브라우저에서 개인 링크 **0개**, 정상
엔지니어링 GitHub 출처 링크 **1개**를 각각 확인했습니다.

**이 수정은 기존 owner-prepared `6118cd2` r2 Stage에 포함되지 않습니다.**
그 Stage를 변경하거나 지우지 마세요. 새 애플리케이션은
`/opt/datarelay-atlas-integrated-candidate-826c266-20261019` 및
`/opt/datarelay-atlas-stage-safe-826c266-20261019`에 준비하도록 고정합니다.
원래 `d561eed` 및 `6118cd2` 후보와 롤백/데이터 백업은 모두 보존합니다.

코드 검증과 별개로 Draft PR 병합, 같은 후보에서 실제 사용자 E2E,
GitHub main 및 배포 아티팩트 정합성, 신뢰된 운영 승인과 실제
MCP 쓰기 검증이 완료돼야 최종 운영 배포 성공을 선언할 수 있습니다.

## 이전 패키지의 실패 상태를 보존해야 합니다

첫 번째 `d561eed` 패키지는 운영 `inspect`와 `prepare`에 성공했지만,
`preflight`에서 Web UI 첫 화면 `/` 응답 지연으로 실패했습니다.
운영 코드 변경이나 실제 `deploy`는 없었습니다.

**이 새 패키지는 그 이전 Stage를 덮어쓰지 않습니다.**
새 애플리케이션 826c266는 첫 화면에서 공통 GitHub Lifecycle 증거를
요청당 한 번만 검증하도록 개선했고, 별도 Operator는 **현재 구버전 첫 화면을 20초 이내 HTTP 200으로 확인**하고
**전환 후 신버전 첫 화면은 8초 이내 HTTP 200을 요구**하면서 `LOOPBACK_WEB_ROOT_TIMEOUT` 원인을 표시합니다.
새 배포용 경로에는 `826c266-20261019` 접미사를 사용하며,
기존 `d561eed-20261018` 후보/Stage/이전 백업·환경을 **절대 삭제하거나
수정하지 마세요.**

이 개선의 효과는 개발 환경에서 검증했지만 운영 Web 응답속도는
아직 재측정하지 못했습니다. 따라서 새 `preflight`가 PASS하기 전에는
`deploy` 명령을 실행하지 마세요.
GitHub Draft PR #354 및 필수 릴리스·병합 승인은 별도이며,
패키지 생성·전달 자체가 운영 전환 승인이나 최종 품질 PASS를 뜻하지 않습니다.

## 정확히 고정한 릴리스

| 항목 | 값 |
| --- | --- |
| Frozen application | 826c26604c4b47b6aab45a889a15b9552985b5ef |
| 추적 파일 | 439개 |
| 소스 아카이브 SHA-256 | d789f25bad4fc7299f7e914a77a2d6901752829c48c7742faba4942e8946a356 |
| 매니페스트 SHA-256 | 379c0d19151ec803b72ca1c7fe3477f3573609ca93a3307a18799d1e4f75c5ba |
| 별도 검증 Operator SHA-256 | 1cfa167b3c231c070558654978777b43b46185ea004716cdda7d79e4520a341b |
| 대상 | prod-atlas, Ubuntu x86_64, Python 3.12 |
| 패키지 | atlas-owner-deploy-826c266-r3-20261019.tar.gz |

패키지에는 deploy.py, 별도 operator.py, 고정된 Git 아카이브,
SHA-256 매니페스트, 오프라인 휠 29개, Python 의존성 고정 목록과 uv 실행기가 포함됩니다.
비밀번호·토큰·인증서·프로덕션 서비스 환경파일은 포함하지 않습니다.

## 승인된 운영 세션에서 실행

사용자 운영 권한과 실제 보안 정책 승인이 선행되어야 합니다. 패키지의 완전한 SHA-256
값은 배포 패키지를 생성한 직후 보고한 값과 일치해야 합니다.
불일치 시 **실행하지 마세요**.

### 1. dev-atlas에서 패키지를 전달

일반 파일 전송은 **별도 승인된 배포 경로**에서만 실행하세요.

~~~sh
sha256sum /home/aella/datarelay-atlas-release-artifacts/atlas-owner-deploy-826c266-r3-20261019.tar.gz
scp /home/aella/datarelay-atlas-release-artifacts/atlas-owner-deploy-826c266-r3-20261019.tar.gz \
  prod-atlas:/home/aella/
~~~

### 2. prod-atlas의 정식 운영 관리 세션에서 압축 해제

인증된 사용자 운영 콘솔에서 **직접** 실행합니다. 패키지를 root 전용 디렉터리로 복사한
뒤 해당 디렉터리에서 실행해야 합니다. deploy.py는 쓰기 가능한 외부 경로의
root 실행을 거부합니다.

~~~sh
sudo install -d -o root -g root -m 0700 /root/atlas-owner-deploy-826c266-r3
sudo tar --no-same-owner --no-same-permissions \
  -xzf /home/aella/atlas-owner-deploy-826c266-r3-20261019.tar.gz \
  -C /root/atlas-owner-deploy-826c266-r3
sudo chmod 0700 /root/atlas-owner-deploy-826c266-r3
sudo python3 /root/atlas-owner-deploy-826c266-r3/deploy.py inspect
~~~

여기까지는 **운영 코드·데이터·서비스가 변경되지 않습니다.**
외부 번들 SHA-256 검증과 내부 아티팩트 검사 결과가 모두 PASS인지 확인하세요.

### 3. 사전 준비: 서비스 중단 없음

**한 단계라도 실패하면 다음 명령을 실행하지 않습니다.** 이전 실패 잔재는 자동 삭제하지
않도록 설계했습니다. 화면에 표시된 실패 코드를 공유하면 해당 경로를 조사할 수 있습니다.

~~~sh
sudo python3 /root/atlas-owner-deploy-826c266-r3/deploy.py prepare
sudo python3 /root/atlas-owner-deploy-826c266-r3/deploy.py preflight
~~~

prepare: 원본 운영 코드 및 롤백 백업의 ID를 확인하고, root 전용 신규 경로에
고정된 소스 및 오프라인 실행환경을 생성합니다. 기존 운영 파일과 서비스는 유지합니다.
preflight: 기존 백업의 **임시 복원**, 스키마 업그레이드·롤백 호환성, 시스템 서비스
계약, 실제 Ingress DynamicUser 샌드박스, 공개 MCP/Keycloak HTTPS와 웹 UI를
검증합니다. 기존 실운영 데이터 루트에서 스키마 업그레이드를 수행하지 않습니다.

### 4. 사용자 확인 후 운영 전환

preflight가 정상 종료된 경우에만 진행하세요.

~~~sh
sudo python3 /root/atlas-owner-deploy-826c266-r3/deploy.py deploy
~~~

**반드시 대화형 터미널(TTY)이 필요**합니다. 표시된 문구를 정확히 입력할 때만
운영 전환이 시작됩니다.

~~~text
DEPLOY 826C266 TO PROD-ATLAS
~~~

동작 순서: 사전 점검 재수행 → 기존 7개 프로젝트의 **현재 상태를 신규 경로에 백업**
→ 백업 복원·스키마 호환성 검증 → Ingress 소켓/서비스 정지 → MCP/Web 정지 →
기존 코드·환경 백업 위치로 이동 → 새 코드·환경 설치 → MCP/Web 시작 →
Ingress 소켓/서비스 재가동 → 서비스 활성화/부팅 지속성/439개 SHA-256 및
공개 MCP·Keycloak HTTPS 검증.

단계 중 문제가 감지되면 기존 코드·서비스 환경·Ingress를 **자동 롤백 시도**하고
정상 여부를 재검증합니다. 복구 실패는 INCIDENT_UNRECOVERED이며 성공으로
보고하지 않습니다. 비정상 전원 종료/SIGKILL에 대한 독립 자동복구는 검증되지 않았습니다.

### 5. 결과 인계

성공 후 터미널 출력 전체와 아래 상태를 공유하세요.
- PREPARED_NO_ACTIVE_CODE_SWITCH 또는 단계별 실패 코드
- PREFLIGHT_QUALIFIED_NO_ACTIVE_CODE_SWITCH 또는 단계별 실패 코드
- CODE_SWITCH_COMPLETED; NATIVE_CHATGPT_MCP_WRITE_E2E_PENDING 또는 롤백 결과

**실제 ChatGPT MCP 쓰기 E2E는 별도**입니다. 운영 전환 자체의 성공만으로
GitHub 소스 등록·메모리 쓰기 사용자 E2E가 완료되었다고 보고하지 않습니다.

## 차단 조건

- 운영 대상 호스트 이름 불일치, root 권한/패키지 디렉터리 소유권 불일치
- Git 아카이브/Operator/휠·잠금파일의 해시 불일치, 439개 추적 파일 불일치
- 기존 운영 코드의 해시 또는 불변 롤백 백업 불일치
- 기존 운영 환경파일과 원본 백업 불일치
- stage/old/failed/fresh-backup 경로가 이미 존재함
- 실제 Ingress 격리/Keycloak/웹 UI/복원 검증 실패
- 비대화형 터미널이나 수동 전환 확인 입력 누락

이 경우 기존 운영 서비스를 유지합니다. **차단 원인을 삭제하거나 기존 백업을
덮어쓰지 마세요.** 운영 권한의 공식 변경/승인 및 장애 대응은 별도 관리 절차입니다.
