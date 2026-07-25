import fs from "node:fs/promises";
import path from "node:path";
import { spawn } from "node:child_process";
import vm from "node:vm";

const DEFAULT_PAGE_URL =
  "https://detail.tmall.com/item.htm?id=1037023345787&item_type=ad&mi_id=0000hantVH_npQO6sllemeOYTPTL23tRP9DVrhmCVvE-TYQ&mm_sceneid=0_0_121472174_0&spm=tbpc.pc_sem_alimama%2Fa.201876.d14";

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

function withTimeout(promise, ms, label) {
  return Promise.race([
    promise,
    new Promise((_, reject) => setTimeout(() => reject(new Error(`${label} timeout`)), ms)),
  ]);
}

function itemPageUrl(itemId, rawPageUrl) {
  for (const candidate of [rawPageUrl, DEFAULT_PAGE_URL]) {
    if (!candidate) continue;
    try {
      const source = new URL(candidate);
      const url = new URL("https://detail.tmall.com/item.htm");
      url.searchParams.set("id", itemId);
      for (const key of ["item_type", "mi_id", "mm_sceneid", "spm"]) {
        const value = source.searchParams.get(key);
        if (value) url.searchParams.set(key, value);
      }
      return url.toString();
    } catch {
      // Try the next template candidate.
    }
  }
  try {
    const source = new URL(DEFAULT_PAGE_URL);
    const url = new URL("https://detail.tmall.com/item.htm");
    url.searchParams.set("id", itemId);
    for (const key of ["item_type", "mi_id", "mm_sceneid", "spm"]) {
      const value = source.searchParams.get(key);
      if (value) url.searchParams.set(key, value);
    }
    return url.toString();
  } catch {
    throw new Error("Invalid Tmall URL template");
  }
}

function walk(value, visitor, pathParts = []) {
  if (!value || typeof value !== "object") return;
  visitor(value, pathParts);
  if (Array.isArray(value)) {
    value.forEach((item, index) => walk(item, visitor, pathParts.concat(index)));
  } else {
    for (const [key, child] of Object.entries(value)) walk(child, visitor, pathParts.concat(key));
  }
}

function parseIceContext(html) {
  const marker = "window.__ICE_APP_CONTEXT__=b;})();";
  const index = String(html || "").indexOf(marker);
  if (index < 0) return null;
  const scriptStart = html.lastIndexOf("<script", index);
  const scriptOpenEnd = html.indexOf(">", scriptStart);
  const scriptClose = html.indexOf("</script>", index);
  if (scriptStart < 0 || scriptOpenEnd < 0 || scriptClose < 0) return null;
  const script = html.slice(scriptOpenEnd + 1, scriptClose);
  const context = { console: { log() {}, warn() {}, error() {} } };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(script, context, { timeout: 5000 });
  return context.__ICE_APP_CONTEXT__ || null;
}

function extractParamValues(root) {
  const result = { huohao: "", xinghao: "", hasParamModule: false, source: "" };
  const setValue = (label, value, source) => {
    const text = Array.isArray(value) ? value.join(" ") : String(value || "");
    const cleaned = text.replace(/\s+/g, " ").trim();
    if (!cleaned) return;
    if (label === "货号" || label === "璐у彿") {
      if (!result.huohao) result.huohao = cleaned;
      result.hasParamModule = true;
      result.source ||= source;
    }
    if (label === "型号" || label === "鍨嬪彿") {
      if (!result.xinghao) result.xinghao = cleaned;
      result.hasParamModule = true;
      result.source ||= source;
    }
  };
  walk(root, (node, pathParts) => {
    const source = pathParts.join(".");
    if (node.propertyName && Object.hasOwn(node, "valueName")) {
      const label = String(node.propertyName);
      if (/货号|型号|璐у彿|鍨嬪彿/.test(label)) setValue(label, node.valueName, source);
      if (/industryParamVO|basicParamList|enhanceParamList/.test(source)) result.hasParamModule = true;
    }
    if (node.title && Object.hasOwn(node, "text")) {
      const label = String(node.title);
      if (/货号|型号|璐у彿|鍨嬪彿/.test(label)) setValue(label, node.text, source);
      if (node.type === "BASE_PROPS" || /BASE_PROPS|extensionInfoVO/.test(source)) result.hasParamModule = true;
    }
  });
  return result;
}

function extractParamsFromHtml(html) {
  try {
    const context = parseIceContext(html);
    if (!context) return { huohao: "", xinghao: "", hasParamModule: false, source: "" };
    return extractParamValues(context);
  } catch {
    return { huohao: "", xinghao: "", hasParamModule: false, source: "" };
  }
}

async function readIds(inputFile) {
  const text = await fs.readFile(inputFile, "utf8");
  return [...new Set(text.match(/\d{10,}/g) || [])];
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
  await fs.mkdir(path.dirname(outputFile), { recursive: true });
  await fs.writeFile(outputFile, `\ufeff${csv}\r\n`, "utf8");
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
  return /RGV587_ERROR|FAIL_SYS_USER_VALIDATE|SESSION|LOGIN|COOKIE|page=登录|page=鐧诲綍/i.test(String(status || ""));
}

async function readExistingRows(outputFile) {
  try {
    const text = await fs.readFile(outputFile, "utf8");
    return parseCsvRows(text).filter((row) => row.itemId && !isValidationStatus(row.status));
  } catch {
    return [];
  }
}

class CdpClient {
  constructor(wsUrl) {
    this.ws = new WebSocket(wsUrl);
    this.nextId = 1;
    this.pending = new Map();
    this.events = [];
    this.ready = new Promise((resolve, reject) => {
      this.ws.addEventListener("open", resolve, { once: true });
      this.ws.addEventListener("error", reject, { once: true });
    });
    this.ws.addEventListener("message", (event) => this.onMessage(event));
  }

  onMessage(event) {
    const msg = JSON.parse(event.data);
    if (msg.method) this.events.push(msg);
    if (!msg.id || !this.pending.has(msg.id)) return;
    const { resolve, reject } = this.pending.get(msg.id);
    this.pending.delete(msg.id);
    msg.error ? reject(new Error(msg.error.message)) : resolve(msg.result);
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

const paramExtractor = String.raw`
(() => {
  const normalize = (value) => String(value || "").replace(/\s+/g, " ").trim();
  const cleanValue = (value) => normalize(value).replace(/^[:：\-\s]+/, "").slice(0, 120);
  const labels = { huohao: "货号", xinghao: "型号" };
  const found = { huohao: "", xinghao: "" };

  function setValue(key, value) {
    const cleaned = cleanValue(value);
    if (!cleaned || cleaned === labels[key]) return;
    if (/^(货号|型号|品牌|材质|颜色分类|适用季节|上市时间)$/.test(cleaned)) return;
    if (!found[key]) found[key] = cleaned;
  }

  function readPairsFromText(text) {
    const lines = String(text || "").split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    for (const [key, label] of Object.entries(labels)) {
      for (let i = 0; i < lines.length; i += 1) {
        const line = lines[i];
        if (line === label) setValue(key, lines[i + 1]);
        const match = line.match(new RegExp("^" + label + "\\s*[:：]?\\s*(.+)$"));
        if (match) setValue(key, match[1]);
      }
    }
  }

  function readPairsFromElements(scope) {
    for (const row of scope.querySelectorAll("tr, li, dl, div, p")) {
      const text = normalize(row.innerText || row.textContent || "");
      if (!/货号|型号/.test(text)) continue;
      for (const [key, label] of Object.entries(labels)) {
        const exact = [...row.querySelectorAll("*")].find((el) => normalize(el.innerText || el.textContent) === label);
        if (exact) {
          const siblings = [...(exact.parentElement?.children || [])];
          const index = siblings.indexOf(exact);
          if (index >= 0) setValue(key, siblings[index + 1]?.innerText || siblings[index + 1]?.textContent);
          setValue(key, exact.nextElementSibling?.innerText || exact.nextElementSibling?.textContent);
        }
        const match = text.match(new RegExp(label + "\\s*[:：]?\\s*([^|；;，,\\n]{2,80})"));
        if (match) setValue(key, match[1]);
      }
    }
  }

  const all = [...document.querySelectorAll("body *")];
  const headings = all.filter((el) => {
    const text = normalize(el.innerText || el.textContent || "");
    return text === "参数信息" || text === "商品参数" || text === "规格参数";
  });

  const modules = [];
  for (const heading of headings) {
    for (let node = heading; node && node !== document.body; node = node.parentElement) {
      const text = normalize(node.innerText || node.textContent || "");
      if (text.includes("参数") && (text.includes("货号") || text.includes("型号"))) {
        modules.push(node);
        break;
      }
    }
    if (heading.nextElementSibling) modules.push(heading.nextElementSibling);
    if (heading.parentElement?.nextElementSibling) modules.push(heading.parentElement.nextElementSibling);
  }

  for (const module of [...new Set(modules)]) {
    readPairsFromElements(module);
    readPairsFromText(module.innerText || module.textContent || "");
  }

  const moduleText = [...new Set(modules)].map((module) => normalize(module.innerText || module.textContent || "")).find(Boolean) || "";
  return {
    href: location.href,
    title: document.title,
    hasParamModule: headings.length > 0 || /参数信息|商品参数|规格参数/.test(document.body?.innerText || ""),
    huohao: found.huohao,
    xinghao: found.xinghao,
    moduleText: moduleText.slice(0, 1200)
  };
})()
`;

async function evaluateJson(cdp, expression) {
  const value = await cdp.send("Runtime.evaluate", {
    expression: `JSON.stringify(${expression})`,
    awaitPromise: true,
    returnByValue: true,
  });
  return JSON.parse(value.result?.value || "{}");
}

async function findDocumentParams(cdp, itemId) {
  const startIndex = cdp.events.length;
  await cdp.send("Network.enable", {
    maxResourceBufferSize: 30_000_000,
    maxTotalBufferSize: 100_000_000,
  });
  return {
    async read() {
      const events = cdp.events.slice(startIndex);
      for (const event of events) {
        const params = event.params || {};
        const response = params.response || {};
        if (params.type !== "Document") continue;
        if (!String(response.url || "").includes("detail.tmall.com/item.htm")) continue;
        if (!String(response.url || "").includes(`id=${itemId}`)) continue;
        try {
          const body = await withTimeout(
            cdp.send("Network.getResponseBody", { requestId: params.requestId }),
            15000,
            "Network.getResponseBody",
          );
          const html = body.base64Encoded ? Buffer.from(body.body, "base64").toString("utf8") : body.body;
          const extracted = extractParamsFromHtml(html);
          return {
            href: response.url,
            title: "",
            hasParamModule: extracted.hasParamModule,
            huohao: extracted.huohao,
            xinghao: extracted.xinghao,
            source: extracted.source ? `DOCUMENT_SSR:${extracted.source}` : "DOCUMENT_SSR",
          };
        } catch {
          // A redirect or evicted response body can fail; keep looking for another document response.
        }
      }
      return {};
    },
    restore() {
      // Network events are recorded on the CDP client; no per-query hook to restore.
    },
  };
}

async function clickAndScrollForParamInfo(cdp, step) {
  await cdp.send("Runtime.evaluate", {
    expression: `
      (() => {
        const targets = ["参数", "参数信息", "商品参数", "规格参数", "详情", "商品详情", "图文详情"];
        for (const el of document.querySelectorAll("a,button,span,div,li")) {
          const text = (el.innerText || el.textContent || "").trim();
          if (targets.includes(text) || /^参数/.test(text) || /参数信息/.test(text)) {
            try { el.click(); } catch {}
          }
        }
        const found = [...document.querySelectorAll("body *")].find((el) => {
          const text = (el.innerText || el.textContent || "").trim();
          return text === "参数信息" || text === "商品参数" || text === "规格参数";
        });
        if (found) found.scrollIntoView({ block: "center" });
        else window.scrollTo(0, Math.min(document.body.scrollHeight, ${step} * 650));
      })()
    `,
    awaitPromise: true,
  });
}

function paramStatus(state) {
  const title = state.title || state.href || "";
  const hasHuohao = Boolean(state.huohao);
  const hasXinghao = Boolean(state.xinghao);
  if (!state.hasParamModule) return `NO_PARAM_INFO_MODULE; page=${title}`;
  if (!hasHuohao && !hasXinghao) return `PARAM_INFO_NO_HUOHAO_XINGHAO; page=${title}`;
  if (hasHuohao && hasXinghao) return `PARAM_INFO_BOTH; page=${title}`;
  return `PARAM_INFO_PARTIAL; missing=${hasHuohao ? "型号" : "货号"}; page=${title}`;
}

async function queryItem(cdp, itemId, rawPageUrl, waitMs, steps) {
  const documentProbe = await findDocumentParams(cdp, itemId);
  await cdp.send("Page.navigate", { url: itemPageUrl(itemId, rawPageUrl) });
  await sleep(3000);
  let best = await documentProbe.read();
  if (best.huohao && best.xinghao) {
    documentProbe.restore();
    return {
      itemId,
      huohao: best.huohao || "",
      xinghao: best.xinghao || "",
      status: paramStatus(best),
    };
  }
  for (let step = 0; step < steps; step += 1) {
    await clickAndScrollForParamInfo(cdp, step);
    await sleep(waitMs);
    const current = await evaluateJson(cdp, paramExtractor);
    if (!best.huohao && current.huohao) best.huohao = current.huohao;
    if (!best.xinghao && current.xinghao) best.xinghao = current.xinghao;
    if (current.hasParamModule) best.hasParamModule = true;
    best.title ||= current.title;
    best.href ||= current.href;
    if (current.huohao && current.xinghao) break;
    if ((best.huohao || best.xinghao) && step > 3) break;
  }
  documentProbe.restore();
  return {
    itemId,
    huohao: best.huohao || "",
    xinghao: best.xinghao || "",
    status: paramStatus(best),
  };
}

async function main() {
  const inputFile = argValue("--input", "work/item-ids.txt");
  const outputFile = argValue("--output", "work/taobao_param_info.csv");
  const rawPageUrl = argValue("--page-url", process.env.TAOBAO_PAGE_URL || DEFAULT_PAGE_URL);
  const delayMs = Number(argValue("--delay", "1000"));
  const jitterMs = Number(argValue("--jitter", "2000"));
  const waitMs = Number(argValue("--param-wait", "850"));
  const steps = Number(argValue("--param-steps", "28"));
  const port = Number(argValue("--port", "9238"));
  const stopOnValidate = hasFlag("--stop-on-validate");
  const headless = hasFlag("--headless");
  const attach = hasFlag("--attach");
  const closeOthers = hasFlag("--close-other-pages");
  const resume = hasFlag("--resume");
  const executablePath = argValue("--browser", "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe");
  const profileDir = argValue("--profile", path.resolve("work", "taobao-cdp-profile"));

  const itemIds = await readIds(inputFile);
  if (!itemIds.length) throw new Error(`No item IDs found in ${inputFile}`);

  const browser = attach ? null : await launchBrowser(executablePath, profileDir, port, headless);
  const cdp = await createPageClient(port);
  if (closeOthers) await closeOtherPages(port, cdp.targetId);
  const rows = resume ? await readExistingRows(outputFile) : [];
  const completedIds = new Set(rows.map((row) => row.itemId));

  try {
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    for (const itemId of itemIds) {
      if (completedIds.has(itemId)) continue;
      const row = await queryItem(cdp, itemId, rawPageUrl, waitMs, steps);
      rows.push(row);
      completedIds.add(itemId);
      await writeCsv(outputFile, rows);
      console.table([row]);
      if (stopOnValidate && /RGV587_ERROR|FAIL_SYS_USER_VALIDATE|SESSION|LOGIN|COOKIE|page=登录|page=鐧诲綍/i.test(row.status)) {
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
