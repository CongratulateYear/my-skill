/* 在已登录的 https://tgc.tmall.com 标签中通过 evaluate 执行。
 * 导航或切换账号后重新注入。凭据仅保留在内存，不打印。
 */
(function () {
  if (location.origin !== "https://tgc.tmall.com") {
    return "请先打开 https://tgc.tmall.com/ds/page/supplier/home 再注入";
  }
  if (window.__dshBridgeRunning) return "bridge agent already running";
  window.__dshBridgeRunning = true;
  const BASE = window.__dshBridgeBase || "http://127.0.0.1:8899";
  const AGENT_ID = crypto.randomUUID();
  const MAX_BINARY = 24 * 1024 * 1024;
  async function local(path, payload) {
    const response = await fetch(BASE + path, payload === undefined ?
      { cache: "no-store" } : {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify(Object.assign({}, payload, { agentId: AGENT_ID })), cache: "no-store"
      });
    if (response.status === 409) window.__dshBridgeRunning = false;
    if (!response.ok) throw new Error("桥响应 " + response.status);
    return response;
  }
  async function reportSession() {
    await local("/session", { cookie: document.cookie || "", href: location.href });
  }
  function toBase64(bytes) {
    let text = "";
    for (let i = 0; i < bytes.length; i += 0x8000) {
      text += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    return btoa(text);
  }
  async function execute(task) {
    if (task.agentId !== AGENT_ID) throw new Error("任务代理不匹配");
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), Math.max(1, task.timeout || 300) * 1000);
    try {
      const url = new URL(task.url);
      if (url.protocol !== "https:" || !["tmall.com", "taobao.com", "alicdn.com", "alibaba.com"].some(
        host => url.hostname === host || url.hostname.endsWith("." + host))) {
        throw new Error("不支持的桥请求地址");
      }
      const headers = Object.assign({}, task.headers || {});
      const token = document.cookie.split(";").map(x => x.trim()).find(
        x => x.startsWith("XSRF-TOKEN=") || x.startsWith("X-XSRF-TOKEN="));
      if (token) headers["x-xsrf-token"] = decodeURIComponent(token.slice(token.indexOf("=") + 1));
      const response = await fetch(task.url, {
        method: task.method || "GET", headers,
        body: task.body, credentials: "include", redirect: "follow",
        cache: "no-store", signal: controller.signal
      });
      const buffer = await response.arrayBuffer();
      if (buffer.byteLength > MAX_BINARY) throw new Error("响应体超过 24MB，请使用签名地址直连下载");
      const contentType = response.headers.get("content-type") || "";
      const isText = /json|text|javascript|xml/i.test(contentType);
      return { status: response.status, url: response.url, contentType,
        disposition: response.headers.get("content-disposition") || "",
        text: isText ? new TextDecoder("utf-8").decode(buffer) : "",
        base64: isText ? "" : toBase64(new Uint8Array(buffer)) };
    } finally { clearTimeout(timer); }
  }
  async function loop() {
    while (window.__dshBridgeRunning) {
      try {
        await reportSession();
        const response = await local("/next?wait=25&agentId=" + encodeURIComponent(AGENT_ID));
        if (response.status === 204) continue;
        const task = await response.json();
        let result;
        try { result = { ok: true, result: await execute(task) }; }
        catch (error) { result = { ok: false, error: String(error.message || error) }; }
        await reportSession();
        await local("/result", Object.assign({ taskId: task.taskId }, result));
      } catch (error) {
        if (!window.__dshBridgeRunning) break;
        await new Promise(resolve => setTimeout(resolve, 2000));
      }
    }
  }
  loop();
  return "bridge agent started";
})();
