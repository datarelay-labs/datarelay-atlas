import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

const MARKER = "ATLAS_BROWSER_RESULT=";

async function readRequest() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

function metrics(state, provider = null, model = null, calls = null) {
  return {
    state, provider, model, llm_call_count: calls,
    input_tokens: null, output_tokens: null, total_tokens: null,
    cost_microusd: null,
  };
}

function baseResult() {
  return {
    result: "HUMAN_REQUIRED", browser_engine: "UNKNOWN",
    browser_version: "UNKNOWN", actual_browser_process: false,
    duration_ms: 0, steps: [], model_metrics: metrics("UNAVAILABLE"),
    trace_ref: null, screenshot_ref: null, error_code: null,
    detail: "Browser verification did not produce evidence.",
  };
}

function emit(result) {
  process.stdout.write(MARKER + JSON.stringify(result) + "\n");
}

function isLoopbackHttpUrl(value) {
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    return false;
  }
  const host = parsed.hostname.toLowerCase();
  return ["http:", "https:"].includes(parsed.protocol)
    && ["127.0.0.1", "localhost", "::1", "[::1]"].includes(host);
}

function assertLoopbackUrl(value, label = "navigation") {
  if (!isLoopbackHttpUrl(value)) {
    throw new Error(label + " left the loopback boundary");
  }
}

async function assertLoopbackPageUrl(page, label) {
  assertLoopbackUrl(page.url(), label);
}

async function installLoopbackRequestGuard(context) {
  await context.route("**/*", async (route) => {
    const value = route.request().url();
    let parsed;
    try {
      parsed = new URL(value);
    } catch {
      await route.abort("blockedbyclient");
      return;
    }
    if (["http:", "https:"].includes(parsed.protocol)
        && !isLoopbackHttpUrl(value)) {
      await route.abort("blockedbyclient");
      return;
    }
    await route.continue();
  });
}

function evidencePaths(request, provider) {
  const safeRun = String(request.run_id).replace(/[^A-Za-z0-9._-]/g, "_");
  const safeProvider = String(provider).replace(/[^A-Za-z0-9._-]/g, "_");
  const safeVariation = String(request.variation).toLowerCase();
  const relDir = path.posix.join("evidence", "browser-verification", safeRun);
  return {
    relDir,
    trace: path.posix.join(relDir, safeProvider + "-" + safeVariation + ".zip"),
    screenshot: path.posix.join(relDir, safeProvider + "-" + safeVariation + ".png"),
  };
}

async function ensureEvidenceDir(paths, repoRoot = process.cwd()) {
  const root = await fsp.realpath(path.resolve(repoRoot));
  const relDir = String(paths.relDir || "");
  const parts = relDir.split("/");
  if (parts.length < 3
      || parts[0] !== "evidence"
      || parts[1] !== "browser-verification"
      || parts.some((part) => !part || part === "." || part === "..")) {
    throw new Error("evidence path is outside the browser verification boundary");
  }

  let current = root;
  for (const part of parts) {
    current = path.join(current, part);
    let stat;
    try {
      stat = await fsp.lstat(current);
    } catch (error) {
      if (!error || error.code !== "ENOENT") throw error;
      await fsp.mkdir(current, { mode: 0o700 });
      stat = await fsp.lstat(current);
    }
    if (stat.isSymbolicLink() || !stat.isDirectory()) {
      throw new Error("evidence path ancestor is unsafe");
    }
    const real = await fsp.realpath(current);
    if (real !== current
        || (real !== root && !real.startsWith(root + path.sep))) {
      throw new Error("evidence path escapes the repository boundary");
    }
  }
}

async function applyVariation(page, variation) {
  if (variation !== "WRAPPED_LAYOUT") return;
  await page.evaluate(() => {
    const link = Array.from(document.querySelectorAll("a")).find(
      (item) => (item.textContent || "").includes("Concurrency"),
    );
    if (!link) throw new Error("concurrency link not found");
    const wrapper = document.createElement("span");
    wrapper.setAttribute("data-atlas-harmless-layout-variation", "1");
    link.replaceWith(wrapper);
    wrapper.appendChild(link);
  });
}

async function verifyOverview(page, steps) {
  const heading = await page.locator("h1").textContent();
  if ((heading || "").trim() !== "Atlas Overview") {
    throw new Error("overview heading mismatch");
  }
  steps.push({
    step_id: "overview_loaded", outcome: "PASS",
    detail: "Atlas Overview rendered in the actual browser.",
    evidence_ref: null,
  });
}

async function verifyConcurrency(page, steps) {
  await page.waitForURL(/\/concurrency(?:$|[?#])/);
  await assertLoopbackPageUrl(page, "concurrency navigation");
  const heading = await page.locator("h1").textContent();
  if ((heading || "").trim() !== "Measured concurrency") {
    throw new Error("concurrency heading mismatch");
  }
  steps.push({
    step_id: "concurrency_loaded", outcome: "PASS",
    detail: "Measured concurrency rendered after browser navigation.",
    evidence_ref: null,
  });
}

async function runPlaywright(request) {
  const start = Date.now();
  const result = baseResult();
  const paths = evidencePaths(request, "playwright");
  let browser = null;
  let context = null;
  try {
    const mod = await import("playwright");
    const chromium = mod.chromium;
    await ensureEvidenceDir(paths);
    browser = await chromium.launch({ headless: true });
    result.actual_browser_process = true;
    result.browser_engine = "chromium";
    result.browser_version = browser.version();
    context = await browser.newContext();
    await installLoopbackRequestGuard(context);
    await context.tracing.start({ screenshots: true, snapshots: true });
    const page = await context.newPage();
    await page.goto(request.target_url, { waitUntil: "domcontentloaded" });
    await assertLoopbackPageUrl(page, "initial navigation");
    await verifyOverview(page, result.steps);
    await applyVariation(page, request.variation);
    result.steps.push({
      step_id: "layout_variation", outcome: "PASS",
      detail: request.variation === "WRAPPED_LAYOUT"
        ? "Harmless wrapper layout variation applied."
        : "Baseline DOM retained.",
      evidence_ref: null,
    });
    await page.getByRole("link", { name: /Concurrency/ }).click();
    await verifyConcurrency(page, result.steps);
    await context.tracing.stop({ path: path.resolve(paths.trace) });
    result.trace_ref = paths.trace;
    result.result = "PASS";
    result.error_code = null;
    result.detail = "Deterministic Playwright journey passed.";
    result.model_metrics = metrics("NOT_APPLICABLE", null, null, 0);
  } catch (error) {
    result.result = result.actual_browser_process ? "FAIL" : "HUMAN_REQUIRED";
    result.error_code = result.actual_browser_process
      ? "PLAYWRIGHT_JOURNEY_FAILED" : "PLAYWRIGHT_RUNTIME_UNAVAILABLE";
    result.detail = result.actual_browser_process
      ? "Deterministic Playwright journey failed."
      : "Playwright or its Chromium runtime is unavailable.";
    if (context) {
      try {
        const pages = context.pages();
        if (pages.length) {
          await ensureEvidenceDir(paths);
          await pages[0].screenshot({
            path: path.resolve(paths.screenshot), fullPage: true,
          });
          result.screenshot_ref = paths.screenshot;
        }
      } catch {}
      try {
        await context.tracing.stop({ path: path.resolve(paths.trace) });
        result.trace_ref = paths.trace;
      } catch {}
    }
  } finally {
    if (browser) { try { await browser.close(); } catch {} }
    result.duration_ms = Date.now() - start;
  }
  return result;
}

function findNumeric(value, names, depth = 0) {
  if (depth > 5 || value === null || value === undefined) return null;
  if (typeof value !== "object") return null;
  for (const entry of Object.entries(value)) {
    const key = entry[0].toLowerCase().replace(/[^a-z]/g, "");
    const candidate = entry[1];
    if (names.includes(key) && typeof candidate === "number"
        && Number.isFinite(candidate) && candidate >= 0) {
      return Math.trunc(candidate);
    }
  }
  for (const candidate of Object.values(value)) {
    const found = findNumeric(candidate, names, depth + 1);
    if (found !== null) return found;
  }
  return null;
}

function stagehandMetrics(metadata, model) {
  const input = findNumeric(metadata, [
    "inputtokens", "prompttokens", "totalprompttokens",
  ]);
  const output = findNumeric(metadata, [
    "outputtokens", "completiontokens", "totalcompletiontokens",
  ]);
  const directTotal = findNumeric(metadata, ["totaltokens"]);
  const total = directTotal !== null
    ? directTotal
    : (input !== null && output !== null ? input + output : null);
  return {
    state: "OBSERVED", provider: "openai", model, llm_call_count: 1,
    input_tokens: input, output_tokens: output, total_tokens: total,
    cost_microusd: null,
  };
}

async function runStagehand(request) {
  const start = Date.now();
  const result = baseResult();
  const paths = evidencePaths(request, "stagehand");
  const model = process.env.ATLAS_STAGEHAND_MODEL || "openai/gpt-5.4-mini";
  result.model_metrics = metrics("UNAVAILABLE", "openai", model, null);
  if (!process.env.OPENAI_API_KEY) {
    result.error_code = "STAGEHAND_MODEL_CREDENTIAL_UNAVAILABLE";
    result.detail =
      "Stagehand semantic execution requires an approved model credential.";
    result.duration_ms = Date.now() - start;
    return result;
  }
  let browser = null;
  let stagehand = null;
  let page = null;
  try {
    const stagehandMod = await import("@browserbasehq/stagehand");
    const playwrightMod = await import("playwright");
    const localBrowser = stagehandMod.localBrowser;
    const Stagehand = stagehandMod.Stagehand;
    const executablePath = playwrightMod.chromium.executablePath();
    if (!fs.existsSync(executablePath)) {
      result.error_code = "STAGEHAND_CHROME_UNAVAILABLE";
      result.detail = "Stagehand local execution requires Chrome.";
      result.duration_ms = Date.now() - start;
      return result;
    }
    browser = await localBrowser.launch({ headless: true, executablePath });
    result.actual_browser_process = true;
    result.browser_engine = "chrome";
    result.browser_version = "STAGEHAND_LOCAL";
    await installLoopbackRequestGuard(browser.context);
    stagehand = await Stagehand.create({
      browser,
      model: { modelName: model, apiKey: process.env.OPENAI_API_KEY },
      logging: { level: "error", format: "pretty" },
    });
    const pages = await browser.context.pages();
    page = pages[0];
    await page.goto(request.target_url);
    await assertLoopbackPageUrl(page, "initial navigation");
    await verifyOverview(page, result.steps);
    await applyVariation(page, request.variation);
    result.steps.push({
      step_id: "layout_variation", outcome: "PASS",
      detail: request.variation === "WRAPPED_LAYOUT"
        ? "Harmless wrapper layout variation applied."
        : "Baseline DOM retained.",
      evidence_ref: null,
    });
    const action = await stagehand.act(
      "Click the link that opens the concurrency view.",
      { page },
    );
    await verifyConcurrency(page, result.steps);
    result.result = "PASS";
    result.error_code = null;
    result.detail = "Stagehand semantic journey passed.";
    result.model_metrics = stagehandMetrics(action && action.metadata, model);
  } catch (error) {
    result.result = result.actual_browser_process ? "FAIL" : "HUMAN_REQUIRED";
    result.error_code = result.actual_browser_process
      ? "STAGEHAND_JOURNEY_FAILED" : "STAGEHAND_RUNTIME_UNAVAILABLE";
    result.detail = result.actual_browser_process
      ? "Stagehand semantic journey failed."
      : "Stagehand optional runtime could not start.";
    if (page) {
      try {
        await ensureEvidenceDir(paths);
        await page.screenshot({
          path: path.resolve(paths.screenshot), fullPage: true,
        });
        result.screenshot_ref = paths.screenshot;
      } catch {}
    }
  } finally {
    if (stagehand) { try { await stagehand.close(); } catch {} }
    if (browser) { try { await browser.close(); } catch {} }
    result.duration_ms = Date.now() - start;
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
      detail: "Browser provider request is invalid JSON.",
    });
    return;
  }
  let result;
  if (request.provider === "playwright") {
    result = await runPlaywright(request);
  } else if (request.provider === "stagehand") {
    result = await runStagehand(request);
  } else {
    result = {
      ...baseResult(),
      error_code: "PROVIDER_UNSUPPORTED",
      detail: "Browser provider is unsupported.",
    };
  }
  emit(result);
}

const invokedAsMain = Boolean(process.argv[1])
  && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url;
if (invokedAsMain) {
  await main();
}

export {
  assertLoopbackUrl,
  ensureEvidenceDir,
  installLoopbackRequestGuard,
};
