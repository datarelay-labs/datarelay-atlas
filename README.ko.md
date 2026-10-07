<h1 align="center">DataRelay Atlas</h1>

<p align="center">
  <strong>Trusted Engineering Context & Lifecycle Control Plane</strong>
</p>

<p align="center">
  Map your engineering. Connect your AI.
</p>

<p align="center">
  <a href="README.md">English</a> · <strong>한국어</strong> ·
  <a href="https://github.com/datarelay-labs/engineering-system">Engineering System</a> ·
  <a href="https://github.com/datarelay-labs/datarelay-atlas-docs">문서</a>
</p>

---

## DataRelay Atlas란?

DataRelay Atlas는 AI-assisted Engineering Knowledge & Lifecycle Platform입니다. **Atlas Core**는 사람과 AI 클라이언트가 동일한 최신 Engineering 상태를 출처와 함께 보도록 만드는 신뢰 가능한 Engineering Context & Lifecycle Control Plane입니다.

Atlas는 Engineering 방법론, 등록된 제품/저장소, Architecture/Specification/ADR, 테스트·릴리즈 증적, 현재 라이프사이클 상태, 프로젝트 간 지식, MCP 기반 AI Context를 연결·검증·검색합니다.

Atlas는 GitHub, CI/CD, Issue Tracker, Coding Agent를 대체하지 않습니다. 선택적인 **Automation Extension**은 Atlas Core의 신뢰 상태를 사용해 AI-assisted Engineering 작업을 조정할 수 있지만, 자동화나 Provider 최적화는 Atlas Core 완성의 필수 조건이 아닙니다.

## 핵심 모델

```text
Canonical Engineering State
GitHub / Specs / Code / Tests / ADR / CI
                    |
                    v
             DataRelay Atlas Core
       Knowledge + Lifecycle + Trust
                    |
          +---------+---------+
          |                   |
          v                   v
      Human UI           AI / MCP Clients
                         ChatGPT / approved clients
                    |
                    +---- optional ----> Automation Extension
```

## 제품 원칙

1. **GitHub가 canonical입니다.** Derived knowledge는 canonical repository state를 덮어쓰지 않습니다.
2. **Engineering System이 방법론을 정의합니다.** Atlas는 이를 적용·관찰·검증·설명하며 별도의 경쟁 방법론을 만들지 않습니다.
3. **Atlas가 Knowledge 동작을 소유합니다.** 과거 Athena/Wiki.js 작업은 migration evidence이며, Atlas는 build/test/runtime에서 Athena 저장소에 의존하지 않습니다 (ADR-0004).
4. **Provenance는 필수입니다.** Derived knowledge에는 가능한 경우 repository/ref/path/source revision을 유지합니다.
5. **초기 범위는 self-hosted single-organization입니다.** Multi-organization/SaaS는 향후 범위이며 MVP 전제가 아닙니다.
6. **이미 잘 동작하는 도구를 다시 만들지 않습니다.** Git hosting, CI/CD, Coding Agent, Issue Tracking은 명시적인 제품 요구가 생기기 전까지 외부 시스템으로 유지합니다.
7. **Core가 automation보다 우선입니다.** Dependency scheduling, Provider routing, Decision Plane, concurrency는 Core state를 소비하는 확장 기능이며 명시적 승격 없이는 Core release blocker가 아닙니다.
8. **Search는 generic chat이 아닙니다.** Atlas는 attributable retrieval/context packaging을 소유하고, 일반적인 답변 합성은 승인된 MCP 클라이언트가 담당합니다.

## 현재 상태

Atlas는 Registry, canonical sync, projection/retrieval, authenticated HTTPS MCP, lifecycle/control-plane foundation, 첫 read-only Human UI를 소유합니다. Verified Continuous Engineering Memory M1의 `get_task_context`는 저장소/워크스트림 작업을 재개할 때 현재 lifecycle/evidence를 작은 bootstrap으로 먼저 제공하고, 더 깊은 내용은 기존 검색·provenance 도구를 JIT로 조회하게 합니다. Athena는 historical migration evidence이며 runtime dependency가 아닙니다.

로드맵은 **Atlas Core / Automation Extension / Experimental-Optional** 세 계층으로 분리됩니다. Core 완성은 기능 개수가 아니라 등록 → sync → attributable retrieval → Human UI/MCP 일치 → lifecycle/release evidence → derived intelligence → production recovery → exact-candidate user-surface gate로 이어지는 end-to-end journey로 판정합니다.

Milestone:

- 제품 계약 + Engineering System adoption
- source/provider/provenance 계약 (ADR-0003)
- Athena absorption inventory + Atlas-native sync/retrieval/MCP context library
- Athena independence gate + retirement checklist (삭제 자체는 owner 승인 후 별도)
- Phase 1 project registry + canonical sync operator surface (`python -m atlas`)

## Phase 1 operator surface

프로젝트를 등록하고 canonical source path를 설정한 뒤 sync/rebuild와 Engineering System adoption metadata 조회를 수행합니다.

```bash
PYTHONPATH=. python3 -m atlas project register datarelay-atlas \
  --repository datarelay-labs/datarelay-atlas
PYTHONPATH=. python3 -m atlas source add datarelay-atlas charter \
  --path docs/product/PRODUCT-CHARTER.md
PYTHONPATH=. python3 -m atlas sync datarelay-atlas
PYTHONPATH=. python3 -m atlas rebuild datarelay-atlas
```

로컬 durable state 기본 경로는 `.atlas-data/`(gitignore)입니다. 자격 증명은 `GITHUB_TOKEN`/런타임 환경만 사용합니다. 상세: `docs/runbooks/phase1-project-registry-canonical-sync.md`.

## ChatGPT에서 활용

[Atlas 사용 지침](integrations/chatgpt-plugin/README.md)은 이전 결정·교훈·프로젝트 문맥이 부족할 때만 필요한 내용을 조회하도록 안내합니다. 이미 충분한 문맥은 재사용하고, Atlas가 응답하지 않거나 최신성이 불명확해도 저장소와 GitHub를 기준으로 개발을 계속합니다. 매 턴 조회, 대화 자동 저장, Work 모드 전환이나 새 권한을 요구하지 않습니다.

## Engineering

이 저장소는 [Data Relay Labs Engineering System](https://github.com/datarelay-labs/engineering-system)을 따르며 `.engineering/project.yaml`에 canonical baseline을 고정합니다.

먼저 확인할 문서:

- `AGENTS.md`
- `.engineering/project.yaml`
- `docs/product/PRODUCT-CHARTER.md`
- `docs/architecture/ARCHITECTURE.md`
- `docs/roadmap/ROADMAP.md`

사용자용 문서는 [datarelay-atlas-docs](https://github.com/datarelay-labs/datarelay-atlas-docs)에서 관리합니다.

## License

현재 dependency/license compatibility audit가 완료되기 전까지 all-rights-reserved 상태입니다. 의도된 정책은 Data Relay Labs의 Source-Available 모델(내부 상업적 사용 허용, 제품화/SaaS 제한)이며, 제3자 구성요소는 각자의 라이선스를 유지합니다.
