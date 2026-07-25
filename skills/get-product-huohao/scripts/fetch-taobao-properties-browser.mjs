import fs from "node:fs/promises";
import path from "node:path";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

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

function itemPageUrl(itemId, rawPageUrl) {
  const fallback = `https://detail.tmall.com/item.htm?id=${itemId}`;
  if (!rawPageUrl) return fallback;
  try {
    const url = new URL(rawPageUrl);
    url.searchParams.set("id", itemId);
    return url.toString();
  } catch {
    return fallback;
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
      cookies.push({
        name,
        value,
        domain,
        path: "/",
        sameSite: "Lax",
        secure: true,
      });
    }
  }
  return cookies;
}

async function readIds(inputFile) {
  const text = await fs.readFile(inputFile, "utf8");
  return [...new Set(text.match(/\d{10,}/g) || [])];
}

function statusFromJson(json, missing) {
  const ret = Array.isArray(json?.ret) ? json.ret.join("; ") : String(json?.ret || "");
  return missing.length ? `${ret || "SUCCESS"}; missing: ${missing.join("/")}` : "OK";
}

function rowFromJson(itemId, json) {
  const found = {};
  for (const property of properties) {
    found[property.key] = findProperty(json, property.label)?.valueName || "";
  }
  const missing = properties.filter((property) => !found[property.key]).map((property) => property.label);
  return {
    itemId,
    huohao: found.huohao,
    xinghao: found.xinghao,
    status: statusFromJson(json, missing),
  };
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

async function queryItem(page, itemId, rawPageUrl, timeoutMs) {
  const url = itemPageUrl(itemId, rawPageUrl);
  const responsePromise = page.waitForResponse(
    (response) => response.url().includes("/mtop.taobao.pcdetail.data.get/"),
    { timeout: timeoutMs },
  ).catch(() => null);

  await page.goto(url, { waitUntil: "domcontentloaded", timeout: timeoutMs }).catch(() => null);
  const response = await responsePromise;
  if (!response) {
    return { itemId, huohao: "", xinghao: "", status: "NO_MTOP_RESPONSE" };
  }

  const text = await response.text();
  const json = unwrapJsonp(text);
  return rowFromJson(itemId, json);
}

async function main() {
  const inputFile = argValue("--input", "work/item-ids.txt");
  const outputFile = argValue("--output", "outputs/taobao_properties.csv");
  const rawPageUrl = argValue("--page-url", process.env.TAOBAO_PAGE_URL || "");
  const delayMs = Number(argValue("--delay", "1000"));
  const jitterMs = Number(argValue("--jitter", "2000"));
  const timeoutMs = Number(argValue("--timeout", "25000"));
  const stopOnValidate = hasFlag("--stop-on-validate");
  const headless = hasFlag("--headless");
  const executablePath = argValue("--browser", "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe");
  const userDataDir = argValue("--profile", path.resolve("work", "taobao-browser-profile"));

  const itemIds = await readIds(inputFile);
  if (!itemIds.length) throw new Error(`No item IDs found in ${inputFile}`);

  const cookie = process.env.TAOBAO_COOKIE || "";
  const context = await chromium.launchPersistentContext(userDataDir, {
    executablePath,
    headless,
    viewport: { width: 1365, height: 900 },
    locale: "zh-CN",
    userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0",
    args: ["--disable-blink-features=AutomationControlled"],
  });

  if (cookie) await context.addCookies(cookiesFromHeader(cookie, rawPageUrl));
  const page = context.pages()[0] || await context.newPage();
  const rows = [];

  try {
    for (const itemId of itemIds) {
      const row = await queryItem(page, itemId, rawPageUrl, timeoutMs);
      rows.push(row);
      await writeCsv(outputFile, rows);
      console.table([row]);
      if (stopOnValidate && /RGV587_ERROR|FAIL_SYS_USER_VALIDATE|SESSION|LOGIN|COOKIE|NO_MTOP_RESPONSE/i.test(row.status)) {
        console.error(`Stopped on validation/error at item ${itemId}: ${row.status}`);
        break;
      }
      const waitMs = delayMs + Math.floor(Math.random() * jitterMs);
      await page.waitForTimeout(waitMs);
    }
  } finally {
    await writeCsv(outputFile, rows);
    await context.close();
  }

  console.log(`Saved: ${outputFile}`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
