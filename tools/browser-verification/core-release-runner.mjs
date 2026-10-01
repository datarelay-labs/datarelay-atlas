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


function headingSurface(targetName, pathname, heading, expectedStatus = 200) {
  return async (context, request) => withPage(context, async (page) => {
    await visit(page, request, targetName, pathname, expectedStatus);
    await requireHeading(page, heading);
    return { status: expectedStatus, detail: heading + " rendered" };
  });
}

function surfaceCases() {
  return {
    overview: headingSurface("primary", "/", "Atlas Overview"),
    intelligence_overview: headingSurface(
      "primary", "/intelligence", "Derived intelligence",
    ),
    operations: headingSurface("primary", "/operations", "Operations readiness"),
    concurrency: headingSurface("primary", "/concurrency", "Measured concurrency"),
    personal: headingSurface("primary", "/personal", "Personal Knowledge Plane"),
    instruction_governance: headingSurface(
      "primary", "/instruction-governance", "Instruction governance",
    ),
    decision_plane: headingSurface("primary", "/decision-plane", "Decision Plane"),
    providers: headingSurface("primary", "/providers", "Provider capacity"),
    search: headingSurface("primary", "/search", "Cross-project search"),
    project: headingSurface("primary", "/projects/core-alpha", "Core Alpha"),
    project_lifecycle: headingSurface(
      "primary", "/projects/core-alpha/lifecycle", "Lifecycle evidence",
    ),
    project_intelligence: headingSurface(
      "primary",
      "/projects/core-alpha/intelligence",
      "Derived engineering intelligence",
    ),
    source_detail: headingSurface(
      "primary",
      "/projects/core-alpha/sources/browser-escape",
      "browser-escape",
    ),
    decision_detail: headingSurface(
      "primary", "/decisions/ADR-9001", "ADR-9001",
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
      if (!body.includes("0 attributable result(s)")) {
        throw new Error("empty search result count is not visible");
      }
      return { status: 200, detail: "empty search rendered zero attributable results" };
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
      await visit(page, request, "primary", "/");
      const response = await page.evaluate(async () => {
        const item = await fetch("/", { method: "POST" });
        return { status: item.status, text: await item.text() };
      });
      if (response.status !== 405 || !response.text.includes("Read-only UI")) {
        throw new Error("browser POST was not rejected as read-only");
      }
      return { status: 405, detail: "browser POST rejected with 405" };
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
        "/projects/core-alpha?q=browser-escape-marker",
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
      return { status: 200, detail: "source markup remained escaped text" };
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
        return "overview -> project -> attributable engineering search";
      },
    ),
    cross_project_retrieval: async (context, request) => withPage(
      context,
      async (page) => {
        await visit(page, request, "primary", "/");
        await page.getByRole("link", { name: /Search across projects/ }).click();
        await page.locator('input[name="q"]').fill("core-product-e2e-marker");
        await page.locator('select[name="source_class"]').selectOption("engineering");
        await page.getByRole("button", { name: "Search" }).click();
        const body = await bodyText(page);
        if (!body.includes("Core Alpha") || !body.includes("Core Beta")) {
          throw new Error("cross-project result groups are incomplete");
        }
        return "explicit engineering search returned both project groups";
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
        if (!body.includes("personal-browser-marker")
            || !body.includes("personal reference / non-authoritative")) {
          throw new Error("personal reference journey lost authority separation");
        }
        return "personal search stayed explicitly non-authoritative";
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
        await page.locator('input[name="q"]').fill("browser-escape-marker");
        await page.getByRole("button", { name: "Search" }).click();
        await requireText(page, "<script>browser-escape-marker</script>");
        if (await page.locator("script").count()) {
          throw new Error("source markup became executable DOM");
        }
        return "source-derived markup remained escaped through project search";
      },
    ),
  };
}


function missionCasesFour() {
  return {
    reload_and_new_context_persistence: async (context, request) => {
      const page = await context.newPage();
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
        const second = await context.newPage();
        try {
          await second.goto(page.url(), { waitUntil: "domcontentloaded" });
          assertLoopbackUrl(second.url(), "new page navigation");
          await requireText(
            second,
            "1111111111111111111111111111111111111111",
          );
        } finally {
          await second.close().catch(() => {});
        }
        return "attributable state persisted across reload and new page";
      } finally {
        await page.close().catch(() => {});
      }
    },
    read_only_post_recovery: async (context, request) => withPage(
      context,
      async (page) => {
        await visit(page, request, "primary", "/");
        const status = await page.evaluate(async () => {
          const response = await fetch("/", { method: "POST" });
          return response.status;
        });
        if (status !== 405) throw new Error("browser POST did not return 405");
        await page.goto(target(request, "primary", "/projects/core-alpha"), {
          waitUntil: "domcontentloaded",
        });
        assertLoopbackUrl(page.url(), "POST recovery navigation");
        await requireHeading(page, "Core Alpha");
        return "read-only rejection preserved subsequent navigation";
      },
    ),
  };
}

async function runMissions(context, request, results) {
  const cases = {
    ...missionCases(),
    ...missionCasesTwo(),
    ...missionCasesThree(),
    ...missionCasesFour(),
  };
  for (const id of request.required_missions) {
    await recordMission(results, id, async () => {
      if (!cases[id]) throw new Error("mission implementation missing");
      return cases[id](context, request);
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
    await runMissions(context, request, result.missions);
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
