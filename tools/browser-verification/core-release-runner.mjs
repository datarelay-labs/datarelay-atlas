import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

import {
  assertLoopbackUrl,
  ensureEvidenceDir,
  installLoopbackRequestGuard,
} from "./runner.mjs";

const MARKER = "ATLAS_CORE_RELEASE_BROWSER_RESULT=";

async function readRequest() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

function emit(result) {
  process.stdout.write(MARKER + JSON.stringify(result) + "\n");
}

function baseResult() {
  return {
    result: "HUMAN_REQUIRED",
    browser_engine: "UNKNOWN",
    browser_version: "UNKNOWN",
    actual_browser_process: false,
    duration_ms: 0,
    surfaces: [],
    missions: [],
    cleanup: { context_closed: true, browser_closed: true },
    trace_ref: null,
    screenshot_ref: null,
    error_code: null,
    detail: "Core release browser verification did not execute.",
  };
}

function shortError(error) {
  const value = error instanceof Error ? error.message : String(error);
  return value.slice(0, 500);
}

function target(request, targetName, pathname) {
  const base = request.targets[targetName];
  if (!base) throw new Error("target is missing: " + targetName);
  const url = new URL(pathname, base);
  assertLoopbackUrl(url.href, "core release navigation");
  return url.href;
}

async function visit(page, request, targetName, pathname, expectedStatus = 200) {
  const response = await page.goto(target(request, targetName, pathname), {
    waitUntil: "domcontentloaded",
  });
  if (!response) throw new Error("navigation produced no HTTP response");
  assertLoopbackUrl(page.url(), "core release navigation");
  if (response.status() !== expectedStatus) {
    throw new Error(
      "expected HTTP " + expectedStatus + " but got " + response.status(),
    );
  }
  return response;
}

async function submitReadOnlyPost(page, request) {
  await visit(page, request, "primary", "/");
  const navigation = page.waitForNavigation({ waitUntil: "domcontentloaded" });
  await page.evaluate(() => {
    const form = document.createElement("form");
    form.method = "POST";
    form.action = "/";
    document.body.appendChild(form);
    form.submit();
  });
  const response = await navigation;
  if (!response || response.status() !== 405) {
    throw new Error("browser form POST was not rejected with 405");
  }
  assertLoopbackUrl(page.url(), "POST rejection navigation");
  await requireHeading(page, "Read-only UI");
  return response;
}

async function bodyText(page) {
  return page.locator("body").innerText();
}

async function requireText(page, value) {
  const body = await bodyText(page);
  if (!body.includes(value)) {
    throw new Error("missing visible text: " + value);
  }
  return body;
}

async function requireHeading(page, value) {
  const heading = ((await page.locator("h1").first().textContent()) || "").trim();
  if (heading !== value) {
    throw new Error("heading mismatch: " + heading);
  }
  return heading;
}

async function withPage(context, fn) {
  const page = await context.newPage();
  try {
    return await fn(page);
  } finally {
    await page.close().catch(() => {});
  }
}

async function recordSurface(results, id, fn) {
  try {
    const observed = await fn();
    results.push({
      surface_id: id,
      outcome: "PASS",
      http_status: observed.status,
      detail: String(observed.detail).slice(0, 500),
    });
  } catch (error) {
    results.push({
      surface_id: id,
      outcome: "FAIL",
      http_status: 599,
      detail: shortError(error),
    });
  }
}

async function recordMission(results, id, fn) {
  try {
    results.push({
      mission_id: id,
      outcome: "PASS",
      detail: String(await fn()).slice(0, 500),
    });
  } catch (error) {
    results.push({
      mission_id: id,
      outcome: "FAIL",
      detail: shortError(error),
    });
  }
}


function contentSurface(
  targetName,
  pathname,
  heading,
  requiredTexts,
  expectedStatus = 200,
) {
  return async (context, request) => withPage(context, async (page) => {
    await visit(page, request, targetName, pathname, expectedStatus);
    await requireHeading(page, heading);
    const body = await bodyText(page);
    for (const text of requiredTexts) {
      if (!body.includes(text)) {
        throw new Error(heading + " missing capability/state: " + text);
      }
    }
    return {
      status: expectedStatus,
      detail: heading + " reconciled with " + requiredTexts.length + " capability/state assertions",
    };
  });
}

function surfaceCases() {
  return {
    overview: async (context, request) => withPage(context, async (page) => {
      await visit(page, request, "primary", "/");
      await requireHeading(page, "Atlas Overview");
      const registered = page.locator("section.card").filter({
        hasText: "Registered projects",
      });
      const enabled = page.locator("section.card").filter({
        hasText: "Enabled projects",
      });
      if ((await registered.locator("h2").textContent() || "").trim() !== "3") {
        throw new Error("overview registered-project count mismatch");
      }
      if ((await enabled.locator("h2").textContent() || "").trim() !== "2") {
        throw new Error("overview enabled-project count mismatch");
      }
      const disabledCard = page.locator("article.card").filter({
        hasText: "Core Disabled",
      });
      if (await disabledCard.count() !== 1
          || (await disabledCard.locator(".pill").first().textContent() || "").trim() !== "disabled"
          || !(await disabledCard.innerText()).includes("datarelay-labs/core-disabled")) {
        throw new Error("overview disabled-project state is not rendered correctly");
      }
      const body = await bodyText(page);
      for (const text of [
        "Configured sources", "Runtime readiness", "Release readiness",
        "Core Alpha", "Core Beta", "Search across projects", "Personal knowledge",
      ]) {
        if (!body.includes(text)) {
          throw new Error("overview missing capability/state: " + text);
        }
      }
      return {
        status: 200,
        detail: "overview counts, enabled/disabled states, inventory and navigation reconciled",
      };
    }),
    intelligence_overview: contentSurface(
      "primary", "/intelligence", "Derived intelligence",
      ["DERIVED", "Summary", "Repository entities", "Decision entities", "Decision index", "Core Alpha"],
    ),
    operations: contentSurface("primary", "/operations", "Operations readiness", [
      "Deployment profile", "Current data-root runtime", "Runbooks",
      "Security controls", "Release readiness", "Runtime observability",
    ]),
    concurrency: contentSurface("primary", "/concurrency", "Measured concurrency", [
      "Admission state", "Dispatch authority", "Measured runs",
      "Admission plan", "Execution cycles",
    ]),
    personal: contentSurface("primary", "/personal", "Personal Knowledge Plane", [
      "PERSONAL_REFERENCE_ONLY", "Personal sources",
      "Engineering sources (excluded from personal search)", "Search personal knowledge",
    ]),
    instruction_governance: contentSurface(
      "primary", "/instruction-governance", "Instruction governance",
      ["Governance state", "Managed surfaces", "Recorded audits", "Mutation authority", "Candidate routing"],
    ),
    decision_plane: contentSurface("primary", "/decision-plane", "Decision Plane", [
      "Shadow/replay measurement only", "Rollout state", "Observations", "Decision classes",
    ]),
    providers: contentSurface("primary", "/providers", "Provider capacity", [
      "Broker plan", "ADVISORY_ONLY", "Failover preview",
      "Verified route outcomes", "Strategy comparison",
    ]),
    search: async (context, request) => withPage(context, async (page) => {
      await visit(page, request, "primary", "/search");
      await requireHeading(page, "Cross-project search");
      const input = page.locator('input[name="q"]');
      const select = page.locator('select[name="source_class"]');
      if (await input.getAttribute("placeholder") !== "Search all registered projects") {
        throw new Error("cross-project search input is not discoverable");
      }
      const options = await select.locator("option").allTextContents();
      if (!options.includes("Engineering only")
          || !options.includes("Personal/reference only")) {
        throw new Error("cross-project source-class controls are incomplete");
      }
      if (await page.locator('form[action="/search"]').count() !== 1) {
        throw new Error("cross-project search form action is missing");
      }
      return {
        status: 200,
        detail: "cross-project search input, source-class controls and form action reconciled",
      };
    }),
    project: contentSurface("primary", "/projects/core-alpha", "Core Alpha", [
      "datarelay-labs/core-alpha", "Configured sources", "Lifecycle evidence",
      "Knowledge coverage", "Sources", "Search knowledge", "browser-escape",
    ]),
    project_lifecycle: contentSurface(
      "primary", "/projects/core-alpha/lifecycle", "Lifecycle evidence",
      ["Work / PR", "CI", "Tests", "Release", "Surface Reconciliation", "Full User E2E"],
    ),
    project_intelligence: contentSurface(
      "primary", "/projects/core-alpha/intelligence",
      "Derived engineering intelligence",
      ["DERIVED", "Decision backlinks", "Knowledge gaps", "Contradictions", "ADR-9001"],
    ),
    source_detail: contentSurface(
      "primary", "/projects/core-alpha/sources/browser-escape", "browser-escape",
      ["Registered source", "Projection", "Validated provenance", "Derived projection content", "docs/browser-escape.md"],
    ),
    decision_detail: contentSurface(
      "primary", "/decisions/ADR-9001", "ADR-9001",
      ["DERIVED", "Decision targets", "Backlinks"],
    ),
  };
}


function customSurfaceCases() {
  return {
    empty_overview: async (context, request) => withPage(context, async (page) => {
      await visit(page, request, "empty", "/");
      await requireHeading(page, "Atlas Overview");
      if (await page.locator('a[href^="/projects/"]').count()) {
        throw new Error("empty overview unexpectedly exposes project links");
      }
      return { status: 200, detail: "empty overview rendered without projects" };
    }),
    empty_search: async (context, request) => withPage(context, async (page) => {
      await visit(page, request, "empty", "/search?q=no-match");
      await requireHeading(page, "Cross-project search");
      const body = await bodyText(page);
      if (!body.includes("No enabled projects.")) {
        throw new Error("empty search project state is not visible");
      }
      return { status: 200, detail: "empty search rendered no enabled projects" };
    }),
    empty_personal: async (context, request) => withPage(context, async (page) => {
      await visit(page, request, "empty", "/personal");
      await requireHeading(page, "Personal Knowledge Plane");
      const body = await bodyText(page);
      if (!body.includes("0") || !body.includes("Personal sources")) {
        throw new Error("empty personal inventory is not visible");
      }
      return { status: 200, detail: "empty personal inventory rendered" };
    }),
    unknown_route: async (context, request) => withPage(context, async (page) => {
      await visit(page, request, "primary", "/route-that-does-not-exist", 404);
      await requireHeading(page, "Not found");
      return { status: 404, detail: "unknown route failed closed" };
    }),
    unknown_project: async (context, request) => withPage(context, async (page) => {
      await visit(page, request, "primary", "/projects/not-registered", 404);
      await requireHeading(page, "Project not found");
      return { status: 404, detail: "unknown project failed closed" };
    }),
    invalid_search: async (context, request) => withPage(context, async (page) => {
      await visit(page, request, "primary", "/search?q=" + "x".repeat(257), 400);
      await requireHeading(page, "Invalid search");
      return { status: 400, detail: "oversized search failed closed" };
    }),
  };
}


function safetySurfaceCases() {
  return {
    corrupt_state: async (context, request) => withPage(context, async (page) => {
      await visit(page, request, "corrupt", "/projects/core-alpha", 500);
      await requireHeading(page, "Atlas state unavailable");
      return { status: 500, detail: "corrupt projection state failed closed" };
    }),
    post_rejection: async (context, request) => withPage(context, async (page) => {
      await submitReadOnlyPost(page, request);
      return { status: 405, detail: "browser form POST rejected with 405" };
    }),
    security_headers: async (context, request) => withPage(context, async (page) => {
      const response = await visit(page, request, "primary", "/");
      const headers = response.headers();
      if (headers["cache-control"] !== "no-store") {
        throw new Error("Cache-Control mismatch");
      }
      if (headers["x-content-type-options"] !== "nosniff") {
        throw new Error("X-Content-Type-Options mismatch");
      }
      if (!(headers["content-security-policy"] || "").includes("default-src 'none'")) {
        throw new Error("Content-Security-Policy mismatch");
      }
      return { status: 200, detail: "no-store, nosniff and CSP observed" };
    }),
  };
}


function trustSurfaceCases() {
  return {
    escaped_source_markup: async (context, request) => withPage(context, async (page) => {
      await visit(
        page,
        request,
        "primary",
        "/projects/core-alpha/sources/browser-escape",
      );
      const body = await requireText(
        page,
        "<script>browser-escape-marker</script>",
      );
      if (await page.locator("script").count()) {
        throw new Error("source-derived script element is executable DOM");
      }
      if (!body.includes("browser-escape-marker")) {
        throw new Error("escaped source marker is absent");
      }
      return { status: 200, detail: "source detail kept markup escaped" };
    }),
    read_only_no_write_controls: async (context, request) => withPage(
      context,
      async (page) => {
        for (const route of ["/", "/projects/core-alpha", "/personal", "/operations"]) {
          await visit(page, request, "primary", route);
          if (await page.locator('form[method="post" i]').count()) {
            throw new Error("POST form exposed at " + route);
          }
          const text = (await bodyText(page)).toLowerCase();
          for (const label of [
            "delete project", "save changes", "create project", "apply changes",
          ]) {
            if (text.includes(label)) {
              throw new Error("mutating control exposed: " + label);
            }
          }
        }
        return { status: 200, detail: "representative Core pages expose no writes" };
      },
    ),
  };
}


function authoritySurfaceCases() {
  return {
    personal_authority_separation: async (context, request) => withPage(
      context,
      async (page) => {
        await visit(
          page,
          request,
          "primary",
          "/personal?q=personal-browser-marker&project=core-alpha",
        );
        const body = await requireText(page, "PERSONAL_REFERENCE_ONLY");
        if (!body.includes("personal reference / non-authoritative")) {
          throw new Error("personal authority label is missing");
        }
        if (body.includes("canonical engineering source reference")) {
          throw new Error("personal content was promoted to engineering authority");
        }
        return { status: 200, detail: "personal content remains reference-only" };
      },
    ),
    lifecycle_truth_states: async (context, request) => withPage(
      context,
      async (page) => {
        await visit(page, request, "primary", "/projects/core-alpha/lifecycle");
        const body = await bodyText(page);
        for (const state of ["OBSERVED", "UNAVAILABLE", "UNKNOWN"]) {
          if (!body.includes(state)) {
            throw new Error("lifecycle truth state is missing: " + state);
          }
        }
        if (!body.includes("Surface Reconciliation")
            || !body.includes("Full User E2E")) {
          throw new Error("human-equivalent release gates are absent");
        }
        return {
          status: 200,
          detail: "OBSERVED, UNAVAILABLE, UNKNOWN and both release gates visible",
        };
      },
    ),
  };
}

async function runSurfaceCases(context, request, results) {
  const cases = {
    ...surfaceCases(),
    ...customSurfaceCases(),
    ...safetySurfaceCases(),
    ...trustSurfaceCases(),
    ...authoritySurfaceCases(),
  };
  for (const id of request.required_surfaces) {
    await recordSurface(results, id, async () => {
      if (!cases[id]) throw new Error("surface case implementation missing");
      return cases[id](context, request);
    });
  }
}


function missionCases() {
  return {
    engineering_navigation_and_search: async (context, request) => withPage(
      context,
      async (page) => {
        await visit(page, request, "primary", "/");
        await page.getByRole("link", { name: "Core Alpha" }).click();
        await page.waitForURL(/\/projects\/core-alpha/);
        assertLoopbackUrl(page.url(), "engineering project navigation");
        await page.locator('input[name="q"]').fill("core-product-e2e-marker");
        await page.getByRole("button", { name: "Search" }).click();
        await page.waitForURL(/q=core-product-e2e-marker/);
        const body = await requireText(page, "docs/architecture.md");
        if (!body.includes("1111111111111111111111111111111111111111")) {
          throw new Error("attributable engineering revision is absent");
        }
        await page.goBack({ waitUntil: "domcontentloaded" });
        assertLoopbackUrl(page.url(), "engineering search back-navigation");
        const backUrl = new URL(page.url());
        if (backUrl.pathname !== "/projects/core-alpha" || backUrl.search) {
          throw new Error("back-navigation did not restore the project view");
        }
        await requireHeading(page, "Core Alpha");
        await page.reload({ waitUntil: "domcontentloaded" });
        assertLoopbackUrl(page.url(), "engineering project reload after back-navigation");
        await requireHeading(page, "Core Alpha");
        const restored = await bodyText(page);
        if (await page.locator('input[name="q"]').inputValue() !== ""
            || !restored.includes("datarelay-labs/core-alpha")
            || !restored.includes("Configured sources")
            || !restored.includes("browser-escape")) {
          throw new Error("project server state was not preserved after back-navigation");
        }
        return "overview -> project -> attributable search -> back with server state preserved";
      },
    ),
    cross_project_retrieval: async (context, request) => withPage(
      context,
      async (page) => {
        const isolated = request.fixture.isolated_query;
        await visit(page, request, "primary", "/");
        await page.getByRole("link", { name: /Search across projects/ }).click();
        await page.locator('input[name="q"]').fill(isolated);
        await page.locator('select[name="source_class"]').selectOption("engineering");
        await page.getByRole("button", { name: "Search" }).click();
        let body = await bodyText(page);
        if (!body.includes("Core Alpha")
            || body.includes("Core Beta")
            || body.includes("Core Disabled")) {
          throw new Error("cross-project isolation did not preserve the sole enabled owning project");
        }
        await page.getByRole("link", { name: "Core Alpha" }).click();
        await page.waitForURL(/\/projects\/core-alpha/);
        assertLoopbackUrl(page.url(), "cross-project owning-group navigation");
        await page.locator('input[name="q"]').fill(isolated);
        await page.getByRole("button", { name: "Search" }).click();
        await page.waitForURL(/q=core-alpha-isolation-marker/);
        body = await bodyText(page);
        if (await page.locator('input[name="q"]').inputValue() !== isolated
            || !body.includes("1 attributable result(s)")
            || !body.includes("datarelay-labs/core-alpha")
            || !body.includes("docs/architecture.md")
            || body.includes("datarelay-labs/core-beta")) {
          throw new Error("project-scoped retrieval leaked or lost owning-project provenance");
        }
        return "one-project-only cross-project search preserved owning-group and project isolation";
      },
    ),
  };
}


function missionCasesTwo() {
  return {
    personal_reference_separation: async (context, request) => withPage(
      context,
      async (page) => {
        await visit(page, request, "primary", "/");
        await page.getByRole("link", { name: /Personal knowledge/ }).click();
        await page.locator('input[name="q"]').fill("personal-browser-marker");
        await page.locator('input[name="project"]').fill("core-alpha");
        await page.getByRole("button", { name: "Search" }).click();
        const body = await bodyText(page);
        if (!body.includes("1 personal/reference result(s)")
            || !body.includes("Browser personal reference")
            || !body.includes("personal reference / non-authoritative")) {
          throw new Error("personal reference journey lost authority separation");
        }
        return "personal search returned one explicit non-authoritative result";
      },
    ),
    lifecycle_observed_unknown_unavailable: async (context, request) => withPage(
      context,
      async (page) => {
        await visit(page, request, "primary", "/projects/core-alpha");
        await page.getByRole("link", { name: /Open lifecycle evidence/ }).click();
        const body = await bodyText(page);
        for (const state of ["OBSERVED", "UNAVAILABLE", "UNKNOWN"]) {
          if (!body.includes(state)) {
            throw new Error("missing lifecycle state: " + state);
          }
        }
        return "independent lifecycle channels preserved mixed truth";
      },
    ),
  };
}


function missionCasesThree() {
  return {
    negative_invalid_recovery: async (context, request) => withPage(
      context,
      async (page) => {
        await visit(
          page,
          request,
          "primary",
          "/search?q=" + "x".repeat(257),
          400,
        );
        await requireHeading(page, "Invalid search");
        await page.getByRole("link", { name: /Back to projects/ }).click();
        await page.waitForURL(/\/$/);
        assertLoopbackUrl(page.url(), "negative recovery navigation");
        await page.getByRole("link", { name: "Core Alpha" }).click();
        await requireHeading(page, "Core Alpha");
        return "invalid search recovered through visible navigation";
      },
    ),
    escaped_markup_safety: async (context, request) => withPage(
      context,
      async (page) => {
        await visit(page, request, "primary", "/projects/core-alpha");
        await page.getByRole("link", { name: "browser-escape" }).click();
        await page.waitForURL(/\/sources\/browser-escape/);
        assertLoopbackUrl(page.url(), "source detail navigation");
        await requireText(page, "<script>browser-escape-marker</script>");
        if (await page.locator("script").count()) {
          throw new Error("source markup became executable DOM");
        }
        return "source-derived markup remained escaped on source detail";
      },
    ),
  };
}


function missionCasesFour() {
  return {
    reload_and_new_context_persistence: async (context, request, browser) => {
      const page = await context.newPage();
      let freshContext = null;
      try {
        await visit(
          page,
          request,
          "primary",
          "/projects/core-alpha?q=core-product-e2e-marker",
        );
        await requireText(page, "1111111111111111111111111111111111111111");
        await page.reload({ waitUntil: "domcontentloaded" });
        assertLoopbackUrl(page.url(), "reload navigation");
        await requireText(page, "1111111111111111111111111111111111111111");

        freshContext = await browser.newContext();
        await installLoopbackRequestGuard(freshContext);
        const freshPage = await freshContext.newPage();
        await freshPage.goto(page.url(), { waitUntil: "domcontentloaded" });
        assertLoopbackUrl(freshPage.url(), "fresh-context navigation");
        await requireText(
          freshPage,
          "1111111111111111111111111111111111111111",
        );
        return "attributable state persisted across reload and a fresh browser context";
      } finally {
        if (freshContext) {
          await freshContext.close().catch(() => {});
        }
        await page.close().catch(() => {});
      }
    },
    read_only_post_recovery: async (context, request) => withPage(
      context,
      async (page) => {
        await submitReadOnlyPost(page, request);
        await page.goto(target(request, "primary", "/projects/core-alpha"), {
          waitUntil: "domcontentloaded",
        });
        assertLoopbackUrl(page.url(), "POST recovery navigation");
        await requireHeading(page, "Core Alpha");
        return "form POST rejection preserved subsequent navigation";
      },
    ),
  };
}

async function runMissions(browser, context, request, results) {
  const cases = {
    ...missionCases(),
    ...missionCasesTwo(),
    ...missionCasesThree(),
    ...missionCasesFour(),
  };
  for (const id of request.required_missions) {
    await recordMission(results, id, async () => {
      if (!cases[id]) throw new Error("mission implementation missing");
      return cases[id](context, request, browser);
    });
  }
}

function fillMissing(result, request) {
  const surfaceIds = new Set(result.surfaces.map((item) => item.surface_id));
  for (const id of request.required_surfaces) {
    if (!surfaceIds.has(id)) {
      result.surfaces.push({
        surface_id: id,
        outcome: "FAIL",
        http_status: 599,
        detail: "surface did not execute",
      });
    }
  }
  const missionIds = new Set(result.missions.map((item) => item.mission_id));
  for (const id of request.required_missions) {
    if (!missionIds.has(id)) {
      result.missions.push({
        mission_id: id,
        outcome: "FAIL",
        detail: "mission did not execute",
      });
    }
  }
}

async function captureFailureScreenshot(context, request, screenshotRef, relDir) {
  try {
    await ensureEvidenceDir({ relDir });
    const page = await context.newPage();
    try {
      await page.goto(target(request, "primary", "/"), {
        waitUntil: "domcontentloaded",
      });
      await page.screenshot({
        path: path.resolve(screenshotRef),
        fullPage: true,
      });
      return screenshotRef;
    } finally {
      await page.close().catch(() => {});
    }
  } catch {
    return null;
  }
}


async function run(request) {
  const started = Date.now();
  const result = baseResult();
  const safeRun = String(request.run_id).replace(/[^A-Za-z0-9._-]/g, "_");
  const relDir = path.posix.join("evidence", "browser-verification", safeRun);
  const traceRef = path.posix.join(relDir, "core-release.zip");
  const screenshotRef = path.posix.join(relDir, "core-release-failure.png");
  let browser = null;
  let context = null;
  let traceStarted = false;

  try {
    const { chromium } = await import("playwright");
    await ensureEvidenceDir({ relDir });
    browser = await chromium.launch({ headless: true });
    result.actual_browser_process = true;
    result.browser_engine = "chromium";
    result.browser_version = browser.version();
    context = await browser.newContext();
    await installLoopbackRequestGuard(context);
    await context.tracing.start({ screenshots: true, snapshots: true });
    traceStarted = true;

    await runSurfaceCases(context, request, result.surfaces);
    await runMissions(browser, context, request, result.missions);
    const failures = [...result.surfaces, ...result.missions]
      .filter((item) => item.outcome !== "PASS");
    if (failures.length) {
      result.error_code = "CORE_RELEASE_BROWSER_FINDINGS";
      result.detail = failures.length + " browser finding(s) remain.";
      result.screenshot_ref = await captureFailureScreenshot(
        context,
        request,
        screenshotRef,
        relDir,
      );
    } else {
      result.detail =
        "Surface Reconciliation and Full User E2E completed in actual Chromium.";
    }
  } catch (error) {
    result.error_code = result.actual_browser_process
      ? "CORE_RELEASE_BROWSER_FAILED"
      : "PLAYWRIGHT_RUNTIME_UNAVAILABLE";
    result.detail = shortError(error);
    if (context) {
      result.screenshot_ref = await captureFailureScreenshot(
        context,
        request,
        screenshotRef,
        relDir,
      );
    }
  } finally {
    fillMissing(result, request);
    if (context && traceStarted) {
      try {
        await context.tracing.stop({ path: path.resolve(traceRef) });
        if (fs.existsSync(path.resolve(traceRef))) {
          result.trace_ref = traceRef;
        }
      } catch {}
    }


    if (context) {
      try {
        await context.close();
        result.cleanup.context_closed = true;
      } catch {
        result.cleanup.context_closed = false;
      }
    }
    if (browser) {
      try {
        await browser.close();
        result.cleanup.browser_closed = !browser.isConnected();
      } catch {
        result.cleanup.browser_closed = false;
      }
    }
    result.duration_ms = Date.now() - started;
  }

  const allPass = [...result.surfaces, ...result.missions]
    .every((item) => item.outcome === "PASS");
  if (
    result.actual_browser_process
    && allPass
    && result.cleanup.context_closed
    && result.cleanup.browser_closed
    && result.trace_ref
  ) {
    result.result = "PASS";
    result.error_code = null;
    result.detail =
      "Surface Reconciliation and Full User E2E passed in actual Chromium with cleanup evidence.";
  } else if (result.actual_browser_process) {
    result.result = "FAIL";
  }
  return result;
}

async function main() {
  let request;
  try {
    request = await readRequest();
  } catch {
    emit({
      ...baseResult(),
      error_code: "REQUEST_INVALID",
      detail: "Core release browser request is invalid JSON.",
    });
    return;
  }
  emit(await run(request));
}

const invokedAsMain = Boolean(process.argv[1])
  && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url;
if (invokedAsMain) {
  await main();
}

export { runSurfaceCases, runMissions };
