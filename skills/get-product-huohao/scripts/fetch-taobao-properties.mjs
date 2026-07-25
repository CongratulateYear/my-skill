import crypto from "node:crypto";
import fs from "node:fs/promises";

const Huohao = "\u8d27\u53f7";
const Xinghao = "\u578b\u53f7";
const properties = [
  { key: "huohao", label: Huohao },
  { key: "xinghao", label: Xinghao },
];
const appKey = "12574478";
const api = "mtop.taobao.pcdetail.data.get";
const version = "1.0";
const endpoint = `https://h5api.m.tmall.com/h5/${api}/${version}/`;
const cookieJar = new Map();

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

function queryParamsForItem(itemId, rawPageUrl) {
  try {
    const url = new URL(itemPageUrl(itemId, rawPageUrl));
    return url.search.startsWith("?") ? url.search.slice(1) : `id=${itemId}`;
  } catch {
    return `id=${itemId}`;
  }
}

function absorbCookie(headerValue) {
  if (!headerValue) return;
  const parts = String(headerValue).split(/,(?=[^;,]+=)/);
  for (const part of parts) {
    const first = part.split(";")[0];
    const eq = first.indexOf("=");
    if (eq > 0) cookieJar.set(first.slice(0, eq).trim(), first.slice(eq + 1).trim());
  }
}

function loadCookie(cookie) {
  for (const part of cookie.split(";")) {
    const eq = part.indexOf("=");
    if (eq > 0) cookieJar.set(part.slice(0, eq).trim(), part.slice(eq + 1).trim());
  }
}

function cookieHeader() {
  return [...cookieJar.entries()].map(([key, value]) => `${key}=${value}`).join("; ");
}

function tokenFromCookie() {
  return (cookieJar.get("_m_h5_tk") || "").split("_")[0] || "";
}

function md5(input) {
  return crypto.createHash("md5").update(input).digest("hex");
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

async function warmDetailPage(itemId, rawPageUrl) {
  const pageUrl = itemPageUrl(itemId, rawPageUrl);
  const response = await fetch(pageUrl, {
    headers: {
      accept: "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
      "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
      "cache-control": "no-cache",
      pragma: "no-cache",
      "sec-ch-ua": "\"Microsoft Edge\";v=\"149\", \"Chromium\";v=\"149\", \"Not)A;Brand\";v=\"24\"",
      "sec-ch-ua-mobile": "?0",
      "sec-ch-ua-platform": "\"Windows\"",
      "sec-fetch-dest": "document",
      "sec-fetch-mode": "navigate",
      "sec-fetch-site": "same-origin",
      "sec-fetch-user": "?1",
      "upgrade-insecure-requests": "1",
      "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0",
      cookie: cookieHeader(),
    },
    redirect: "follow",
  });
  absorbCookie(response.headers.get("set-cookie"));
  await response.arrayBuffer();
}

async function callDetail(itemId, rawPageUrl) {
  const pageUrl = itemPageUrl(itemId, rawPageUrl);
  const page = new URL(pageUrl);
  const dataObject = {
    id: itemId,
    detail_v: "3.3.2",
    exParams: JSON.stringify({
      id: itemId,
      queryParams: queryParamsForItem(itemId, rawPageUrl),
      domain: `${page.protocol}//${page.host}`,
      path_name: page.pathname,
    }),
  };

  const data = JSON.stringify(dataObject);
  const t = Date.now().toString();
  const sign = md5(`${tokenFromCookie()}&${t}&${appKey}&${data}`);
  const url = new URL(endpoint);
  url.search = new URLSearchParams({
    jsv: "2.7.2",
    appKey,
    t,
    sign,
    api,
    v: version,
    type: "jsonp",
    dataType: "jsonp",
    callback: "mtopjsonp1",
    data,
  }).toString();

  const response = await fetch(url, {
    headers: {
      accept: "*/*",
      "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
      "cache-control": "no-cache",
      pragma: "no-cache",
      referer: pageUrl,
      "sec-ch-ua": "\"Microsoft Edge\";v=\"149\", \"Chromium\";v=\"149\", \"Not)A;Brand\";v=\"24\"",
      "sec-ch-ua-mobile": "?0",
      "sec-ch-ua-platform": "\"Windows\"",
      "sec-fetch-dest": "script",
      "sec-fetch-mode": "no-cors",
      "sec-fetch-site": "same-site",
      "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0",
      cookie: cookieHeader(),
    },
  });

  absorbCookie(response.headers.get("set-cookie"));
  return unwrapJsonp(await response.text());
}

async function queryItem(itemId, rawPageUrl, warmPage) {
  if (warmPage) await warmDetailPage(itemId, rawPageUrl);
  let json = await callDetail(itemId, rawPageUrl);
  let ret = Array.isArray(json.ret) ? json.ret.join("; ") : String(json.ret || "");

  if (/TOKEN|FAIL_SYS_TOKEN/i.test(ret)) {
    json = await callDetail(itemId, rawPageUrl);
    ret = Array.isArray(json.ret) ? json.ret.join("; ") : String(json.ret || "");
  }

  const found = {};
  for (const property of properties) {
    found[property.key] = findProperty(json, property.label)?.valueName || "";
  }

  const missing = properties.filter((property) => !found[property.key]).map((property) => property.label);
  const status = missing.length
    ? `${ret || "SUCCESS"}; missing: ${missing.join("/")}`
    : "OK";

  return {
    itemId,
    huohao: found.huohao,
    xinghao: found.xinghao,
    status,
  };
}

function csvEscape(value) {
  const s = String(value ?? "");
  return /[",\r\n]/.test(s) ? `"${s.replaceAll('"', '""')}"` : s;
}

async function readIds(inputFile) {
  const text = await fs.readFile(inputFile, "utf8");
  return [...new Set(text.match(/\d{10,}/g) || [])];
}

async function main() {
  const inputFile = argValue("--input", "work/item-ids.txt");
  const outputFile = argValue("--output", "outputs/taobao_properties.csv");
  const delayMs = Number(argValue("--delay", "1000"));
  const jitterMs = Number(argValue("--jitter", "2000"));
  const pageUrl = argValue("--page-url", process.env.TAOBAO_PAGE_URL || "");
  const warmPage = hasFlag("--warm-page");
  const stopOnValidate = hasFlag("--stop-on-validate");
  const itemIds = await readIds(inputFile);

  if (!itemIds.length) throw new Error(`No item IDs found in ${inputFile}`);
  loadCookie(process.env.TAOBAO_COOKIE || "");

  const rows = [];
  for (const itemId of itemIds) {
    try {
      const row = await queryItem(itemId, pageUrl, warmPage);
      rows.push(row);
      if (stopOnValidate && /RGV587_ERROR|FAIL_SYS_USER_VALIDATE|SESSION|LOGIN|COOKIE/i.test(row.status)) {
        console.error(`Stopped on validation error at item ${itemId}: ${row.status}`);
        break;
      }
    } catch (error) {
      const row = { itemId, huohao: "", xinghao: "", status: `ERROR: ${error.message}` };
      rows.push(row);
      if (stopOnValidate && /RGV587_ERROR|FAIL_SYS_USER_VALIDATE|SESSION|LOGIN|COOKIE/i.test(row.status)) {
        console.error(`Stopped on validation error at item ${itemId}: ${row.status}`);
        break;
      }
    }
    const waitMs = delayMs + Math.floor(Math.random() * jitterMs);
    await new Promise((resolve) => setTimeout(resolve, waitMs));
  }

  const header = ["itemId", "huohao", "xinghao", "status"];
  const csv = [
    header.join(","),
    ...rows.map((row) => header.map((key) => csvEscape(row[key])).join(",")),
  ].join("\r\n");

  await fs.writeFile(outputFile, `\ufeff${csv}\r\n`, "utf8");
  console.table(rows);
  console.log(`Saved: ${outputFile}`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
