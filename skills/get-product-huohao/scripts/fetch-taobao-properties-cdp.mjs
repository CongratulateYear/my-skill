import fs from "node:fs/promises";
import path from "node:path";
import { spawn } from "node:child_process";

const Huohao = "\u8d27\u53f7";
const Xinghao = "\u578b\u53f7";
const properties = [
  { key: "huohao", label: Huohao },
  { key: "xinghao", label: Xinghao },
];

function argValue(name, fallback = "") {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] || fallback : fallback;
}

function hasFlag(name) {
  return process.argv.includes(name);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function itemPageUrl(itemId, rawPageUrl, skuId = "") {
  const fallback = `https://detail.tmall.com/item.htm?id=${itemId}`;
  if (!rawPageUrl) return skuId ? `${fallback}&skuId=${skuId}` : fallback;
  try {
    const url = new URL(rawPageUrl);
    url.searchParams.set("id", itemId);
    if (skuId) url.searchParams.set("skuId", skuId);
    url.searchParams.delete("sku_properties");
    return url.toString();
  } catch {
    return skuId ? `${fallback}&skuId=${skuId}` : fallback;
  }
}

function unwrapJsonp(text) {
  const trimmed = text.trim();
  const match = trimmed.match(/^[^(]+\((.*)\);?$/s);
  return JSON.parse(match ? match[1] : trimmed);
}

function findProperty(obj, propertyName) {
  if (!obj || typeof obj !== "object") return null;
  if (obj.propertyName === propertyName && obj.valueName != null) return obj;
  const values = Array.isArray(obj) ? obj : Object.values(obj);
  for (const value of values) {
    const found = findProperty(value, propertyName);
    if (found) return found;
  }
  return null;
}

function cookieDomainsFromPage(rawPageUrl) {
  const domains = new Set([".tmall.com", ".taobao.com"]);
  try {
    const host = new URL(rawPageUrl).hostname;
    domains.add(host);
    const parts = host.split(".");
    if (parts.length >= 2) domains.add(`.${parts.slice(-2).join(".")}`);
  } catch {
    // Keep defaults.
  }
  return [...domains];
}

function cookiesFromHeader(cookieHeader, rawPageUrl) {
  const cookies = [];
  const domains = cookieDomainsFromPage(rawPageUrl);
  for (const part of cookieHeader.split(";")) {
    const eq = part.indexOf("=");
    if (eq <= 0) continue;
    const name = part.slice(0, eq).trim();
    const value = part.slice(eq + 1).trim();
    if (!name) continue;
    for (const domain of domains) {
      cookies.push({ name, value, domain, path: "/", secure: true });
    }
  }
  return cookies;
}

async function readIds(inputFile) {
  const text = await fs.readFile(inputFile, "utf8");
  return [...new Set(text.match(/\d{10,}/g) || [])];
}

function parseCsvRows(text) {
  const rows = [];
  let row = [];
  let field = "";
  let quoted = false;
  const input = String(text || "").replace(/^\ufeff/, "");
  for (let index = 0; index < input.length; index += 1) {
    const char = input[index];
    const next = input[index + 1];
    if (quoted) {
      if (char === '"' && next === '"') {
        field += '"';
        index += 1;
      } else if (char === '"') {
        quoted = false;
      } else {
        field += char;
      }
    } else if (char === '"') {
      quoted = true;
    } else if (char === ",") {
      row.push(field);
      field = "";
    } else if (char === "\n") {
      row.push(field);
      rows.push(row);
      row = [];
      field = "";
    } else if (char !== "\r") {
      field += char;
    }
  }
  if (field || row.length) {
    row.push(field);
    rows.push(row);
  }
  const [header = [], ...dataRows] = rows.filter((fields) => fields.some((value) => value !== ""));
  return dataRows.map((fields) => Object.fromEntries(header.map((key, index) => [key, fields[index] || ""])));
}

function isValidationStatus(status) {
  return /RGV587_ERROR|FAIL_SYS_USER_VALIDATE|SESSION|LOGIN|COOKIE|page=登录|page=鐧/i.test(String(status || ""));
}

async function readExistingRows(outputFile) {
  try {
    const text = await fs.readFile(outputFile, "utf8");
    return parseCsvRows(text).filter((row) => row.itemId && !isValidationStatus(row.status));
  } catch {
    return [];
  }
}

function rowFromJson(itemId, json) {
  const found = {};
  for (const property of properties) {
    found[property.key] = findProperty(json, property.label)?.valueName || "";
  }
  const missing = properties.filter((property) => !found[property.key]).map((property) => property.label);
  const ret = Array.isArray(json?.ret) ? json.ret.join("; ") : String(json?.ret || "");
  return {
    itemId,
    huohao: found.huohao,
    xinghao: found.xinghao,
    status: missing.length ? `${ret || "SUCCESS"}; missing: ${missing.join("/")}` : "OK",
  };
}

function valueAfterLabel(text, label) {
  const lines = String(text || "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  for (let index = 0; index < lines.length; index += 1) {
    if (lines[index] === label) return lines[index + 1] || "";
    if (lines[index].startsWith(`${label}:`)) return lines[index].slice(label.length + 1).trim();
    if (lines[index].startsWith(`${label}：`)) return lines[index].slice(label.length + 1).trim();
  }
  return "";
}

function rowFromPageText(itemId, text, pageTitle = "") {
  const found = {
    huohao: valueAfterLabel(text, Huohao),
    xinghao: valueAfterLabel(text, Xinghao),
  };
  const missing = properties.filter((property) => !found[property.key]).map((property) => property.label);
  return {
    itemId,
    huohao: found.huohao,
    xinghao: found.xinghao,
    status: missing.length ? `DOM_FALLBACK; page=${pageTitle || "loaded"}; missing: ${missing.join("/")}` : "OK",
  };
}

async function pageState(cdp) {
  const evaluated = await cdp.send("Runtime.evaluate", {
    expression: "JSON.stringify({href: location.href, title: document.title, readyState: document.readyState, body: document.body && document.body.innerText, skuIds:[...new Set([...document.documentElement.innerHTML.matchAll(/skuId[\\\"'=:\\\\s]+(\\\\d{8,})/g)].map(m=>m[1]))].slice(0,12)})",
    returnByValue: true,
  });
  return JSON.parse(evaluated.result?.value || "{}");
}

async function waitForDomRow(cdp, itemId, domWaitMs) {
  const deadline = Date.now() + domWaitMs;
  let lastState = {};
  while (Date.now() < deadline) {
    try {
      lastState = await pageState(cdp);
    } catch {
      lastState = {};
    }

    if ((lastState.href || "").includes(itemId)) {
      const domRow = rowFromPageText(itemId, lastState.body || "", lastState.title || lastState.href);
      if (domRow.huohao || domRow.xinghao) return { row: domRow, state: lastState };
      if (lastState.readyState === "complete" && (lastState.body || "").length > 500) break;
    }
    await sleep(500);
  }
  return { row: null, state: lastState };
}

function csvEscape(value) {
  const s = String(value ?? "");
  return /[",\r\n]/.test(s) ? `"${s.replaceAll('"', '""')}"` : s;
}

async function writeCsv(outputFile, rows) {
  const header = ["itemId", "huohao", "xinghao", "status"];
  const csv = [
    header.join(","),
    ...rows.map((row) => header.map((key) => csvEscape(row[key])).join(",")),
  ].join("\r\n");
  await fs.writeFile(outputFile, `\ufeff${csv}\r\n`, "utf8");
}

async function waitForJson(port, pathName, timeoutMs = 10000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}${pathName}`);
      if (response.ok) return await response.json();
    } catch {
      // Browser is still starting.
    }
    await sleep(250);
  }
  throw new Error(`CDP endpoint not ready: ${pathName}`);
}

class CdpClient {
  constructor(wsUrl) {
    this.ws = new WebSocket(wsUrl);
    this.nextId = 1;
    this.pending = new Map();
    this.handlers = new Map();
    this.ready = new Promise((resolve, reject) => {
      this.ws.addEventListener("open", resolve, { once: true });
      this.ws.addEventListener("error", reject, { once: true });
    });
    this.ws.addEventListener("message", (event) => this.onMessage(event));
  }

  onMessage(event) {
    const msg = JSON.parse(event.data);
    if (msg.id && this.pending.has(msg.id)) {
      const { resolve, reject } = this.pending.get(msg.id);
      this.pending.delete(msg.id);
      if (msg.error) reject(new Error(msg.error.message));
      else resolve(msg.result);
      return;
    }
    if (msg.method && this.handlers.has(msg.method)) {
      for (const handler of this.handlers.get(msg.method)) handler(msg.params || {});
    }
  }

  on(method, handler) {
    if (!this.handlers.has(method)) this.handlers.set(method, []);
    this.handlers.get(method).push(handler);
  }

  async send(method, params = {}) {
    await this.ready;
    const id = this.nextId++;
    const result = new Promise((resolve, reject) => this.pending.set(id, { resolve, reject }));
    this.ws.send(JSON.stringify({ id, method, params }));
    return result;
  }

  close() {
    this.ws.close();
  }
}

async function launchBrowser(executablePath, profileDir, port, headless) {
  await fs.mkdir(profileDir, { recursive: true });
  const args = [
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profileDir}`,
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-blink-features=AutomationControlled",
    "about:blank",
  ];
  if (headless) args.unshift("--headless=new");
  return spawn(executablePath, args, { stdio: "ignore", detached: false });
}

async function createPageClient(port) {
  await waitForJson(port, "/json/version");
  let target = null;
  try {
    const response = await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: "PUT" });
    const text = await response.text();
    if (response.ok) target = JSON.parse(text);
  } catch {
    target = null;
  }
  if (!target?.webSocketDebuggerUrl) {
    const pages = await fetch(`http://127.0.0.1:${port}/json/list`).then((r) => r.json());
    target = pages.find((page) => page.type === "page" && page.webSocketDebuggerUrl);
  }
  if (!target?.webSocketDebuggerUrl) throw new Error("No controllable CDP page target found");
  const client = new CdpClient(target.webSocketDebuggerUrl);
  client.targetId = target.id;
  return client;
}

async function closeOtherPages(port, keepTargetId = "") {
  const pages = await fetch(`http://127.0.0.1:${port}/json/list`).then((r) => r.json());
  for (const page of pages) {
    if (page.type === "page" && page.id !== keepTargetId) {
      await fetch(`http://127.0.0.1:${port}/json/close/${page.id}`).catch(() => null);
    }
  }
}

async function queryItem(cdp, itemId, rawPageUrl, timeoutMs, domWaitMs, skuLimit) {
  const url = itemPageUrl(itemId, rawPageUrl);
  let targetRequestId = null;
  let responseText = null;
  const interestingUrls = [];

  const mtopDone = new Promise((resolve) => {
    const timer = setTimeout(() => resolve(null), timeoutMs);
    timer.unref?.();
    cdp.on("Network.requestWillBeSent", (params) => {
      const requestUrl = params.request?.url || "";
      if (/mtop|h5api|login|captcha|punish|____tmd_____|detail\.tmall|item\.htm/i.test(requestUrl)) {
        interestingUrls.push(requestUrl);
      }
    });
    cdp.on("Network.responseReceived", (params) => {
      if (params.response?.url?.includes("/mtop.taobao.pcdetail.data.get/")) {
        targetRequestId = params.requestId;
      }
    });
    cdp.on("Network.loadingFinished", async (params) => {
      if (targetRequestId && params.requestId === targetRequestId) {
        try {
          const body = await cdp.send("Network.getResponseBody", { requestId: targetRequestId });
          responseText = body.base64Encoded ? Buffer.from(body.body, "base64").toString("utf8") : body.body;
        } catch (error) {
          responseText = JSON.stringify({ ret: [`ERROR::${error.message}`] });
        }
        clearTimeout(timer);
        resolve(responseText);
      }
    });
  });

  await cdp.send("Page.navigate", { url });
  let { row: domRow, state: lastState } = await waitForDomRow(cdp, itemId, domWaitMs);
  if (domRow) return domRow;

  const skuIds = lastState.skuIds || [];
  for (const skuId of skuIds.slice(0, skuLimit)) {
    await cdp.send("Page.navigate", { url: itemPageUrl(itemId, rawPageUrl, skuId) });
    const result = await waitForDomRow(cdp, itemId, domWaitMs);
    if (result.row) {
      result.row.status = result.row.status === "OK"
        ? `OK; skuId=${skuId}`
        : `${result.row.status}; skuId=${skuId}`;
      return result.row;
    }
    lastState = result.state || lastState;
  }

  const text = await Promise.race([mtopDone, sleep(1000).then(() => null)]);
  if (!text) {
    const pageState = lastState || {};
    const domRow = rowFromPageText(itemId, pageState.body || "", pageState.title || pageState.href);
    if (domRow.huohao || domRow.xinghao) return domRow;
    return {
      itemId,
      huohao: "",
      xinghao: "",
      status: `NO_MTOP_RESPONSE; page=${pageState.title || pageState.href || "unknown"}; urls=${interestingUrls.length}`,
      debug: { pageState, interestingUrls: [...new Set(interestingUrls)].slice(-50) },
    };
  }
  return rowFromJson(itemId, unwrapJsonp(text));
}

async function main() {
  const inputFile = argValue("--input", "work/item-ids.txt");
  const outputFile = argValue("--output", "outputs/taobao_properties.csv");
  const rawPageUrl = argValue("--page-url", process.env.TAOBAO_PAGE_URL || "");
  const delayMs = Number(argValue("--delay", "1000"));
  const jitterMs = Number(argValue("--jitter", "2000"));
  const timeoutMs = Number(argValue("--timeout", "30000"));
  const domWaitMs = Number(argValue("--dom-wait", "8000"));
  const skuLimit = Number(argValue("--sku-limit", "5"));
  const port = Number(argValue("--port", "9233"));
  const debugFile = argValue("--debug-file", "");
  const stopOnValidate = hasFlag("--stop-on-validate");
  const headless = hasFlag("--headless");
  const attach = hasFlag("--attach");
  const closeOthers = hasFlag("--close-other-pages");
  const resume = hasFlag("--resume");
  const executablePath = argValue("--browser", "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe");
  const profileDir = argValue("--profile", path.resolve("work", "taobao-cdp-profile"));

  const itemIds = await readIds(inputFile);
  if (!itemIds.length) throw new Error(`No item IDs found in ${inputFile}`);

  const browser = attach ? null : await launchBrowser(executablePath, profileDir, port, headless);
  const cdp = await createPageClient(port);
  if (closeOthers) await closeOtherPages(port, cdp.targetId);
  const rows = resume ? await readExistingRows(outputFile) : [];
  const completedIds = new Set(rows.map((row) => row.itemId));

  try {
    await cdp.send("Network.enable");
    await cdp.send("Page.enable");
    const cookie = process.env.TAOBAO_COOKIE || "";
    if (cookie) await cdp.send("Network.setCookies", { cookies: cookiesFromHeader(cookie, rawPageUrl) });

    for (const itemId of itemIds) {
      if (completedIds.has(itemId)) continue;
      const row = await queryItem(cdp, itemId, rawPageUrl, timeoutMs, domWaitMs, skuLimit);
      if (debugFile && row.debug) {
        await fs.mkdir(path.dirname(debugFile), { recursive: true });
        await fs.writeFile(debugFile, JSON.stringify(row.debug, null, 2), "utf8");
        delete row.debug;
      }
      rows.push(row);
      completedIds.add(itemId);
      await writeCsv(outputFile, rows);
      console.table([row]);
      if (stopOnValidate && /RGV587_ERROR|FAIL_SYS_USER_VALIDATE|SESSION|LOGIN|COOKIE|page=登录/i.test(row.status)) {
        console.error(`Stopped on validation/error at item ${itemId}: ${row.status}`);
        break;
      }
      await sleep(delayMs + Math.floor(Math.random() * jitterMs));
    }
  } finally {
    await writeCsv(outputFile, rows);
    cdp.close();
    if (browser) browser.kill();
  }
  console.log(`Saved: ${outputFile}`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
