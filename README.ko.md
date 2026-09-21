<h1 align="center">DataRelay Atlas</h1>

<p align="center">
  <strong>Engineering Knowledge & Lifecycle Platform</strong>
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

DataRelay Atlas는 프로젝트 지식, Engineering 방법론, 라이프사이클 상태, AI Context를 연결하는 AI-assisted Engineering Platform입니다.

사람과 AI가 Engineering 방법론, 제품/저장소, Architecture/Specification/ADR, 테스트·릴리즈 증적, 현재 개발 상태, 프로젝트 간 지식, MCP 기반 AI Context를 일관되게 이해하도록 만드는 것이 목적입니다.

Atlas는 GitHub, CI/CD, Issue Tracker, Coding Agent를 대체하지 않습니다. 기존 시스템에 존재하는 Engineering State를 연결하고 검증하고 검색 가능하게 만들며 설명합니다.

## 핵심 모델

```text
Canonical Engineering State
GitHub / OpenSpec / Code / Tests / ADR / CI
                    |
                    v
              DataRelay Atlas
       Methodology + Project State
       Knowledge + AI Context
                    |
          +---------+---------+
          |                   |
          v                   v
      Human UI           AI / MCP Clients
                         Cursor / ChatGPT
```

## 제품 원칙

1. **GitHub가 canonical입니다.** Derived knowledge는 canonical repository state를 덮어쓰지 않습니다.
2. **Engineering System이 방법론을 정의합니다.** Atlas는 이를 적용·관찰·검증·설명하며 별도의 경쟁 방법론을 만들지 않습니다.
3. **Atlas가 Knowledge 동작을 소유합니다.** 과거 Athena/Wiki.js 작업은 migration evidence이며, Atlas는 build/test/runtime에서 Athena 저장소에 의존하지 않습니다 (ADR-0004).
4. **Provenance는 필수입니다.** Derived knowledge에는 가능한 경우 repository/ref/path/source revision을 유지합니다.
5. **초기 범위는 self-hosted single-organization입니다.** Multi-organization/SaaS는 향후 범위이며 MVP 전제가 아닙니다.
6. **이미 잘 동작하는 도구를 다시 만들지 않습니다.** Git hosting, CI/CD, Coding Agent, Issue Tracking은 명시적인 제품 요구가 생기기 전까지 외부 시스템으로 유지합니다.

## 현재 상태

DataRelay Atlas는 Engineering Knowledge PoC에서 필요한 능력을 Atlas 소유 코드로 흡수하고, Athena를 제품 의존성에서 제거하는 작업 중입니다 (ADR-0004). Phase 1은 로컬 Project Registry와 authenticated canonical sync를 추가합니다 (ADR-0005).

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
