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
3. **Knowledge Engine은 교체 가능합니다.** Athena는 Wiki/Search/Vector/MCP 기반 기능을 제공할 수 있지만 제품 경계는 DataRelay Atlas입니다.
4. **Provenance는 필수입니다.** Derived knowledge에는 가능한 경우 repository/ref/path/source revision을 유지합니다.
5. **초기 범위는 self-hosted single-organization입니다.** Multi-organization/SaaS는 향후 범위이며 MVP 전제가 아닙니다.
6. **이미 잘 동작하는 도구를 다시 만들지 않습니다.** Git hosting, CI/CD, Coding Agent, Issue Tracking은 명시적인 제품 요구가 생기기 전까지 외부 시스템으로 유지합니다.

## 현재 상태

현재는 초기 제품 정의 및 Architecture bootstrap 단계입니다.

첫 번째 milestone은 제품 계약, Engineering System adoption, Knowledge Architecture, 기존 Athena PoC에서 제품으로 이동하기 위한 bounded migration path를 확정하는 것입니다.

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
