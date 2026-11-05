/* 在已登录的 https://www.erp321.com 标签中通过 evaluate 执行。
 * 导航或切换账号后重新注入。凭据仅保留在内存，不打印。
 */
(function () {
  if (location.origin !== "https://www.erp321.com") {
    return "请先打开 https://www.erp321.com/epaas 再注入";
  }
  if (window.__dshJstBridgeRunning) return "bridge agent already running";
  window.__dshJstBridgeRunning = true;
  const BASE = window.__dshJstBridgeBase || "http://127.0.0.1:8901";
  const AGENT_ID = crypto.randomUUID();
  const MAX_BINARY = 24 * 1024 * 1024;
  async function local(path, payload) {
    const response = await fetch(BASE + path, payload === undefined ?
      { cache: "no-store" } : {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify(Object.assign({}, payload, { agentId: AGENT_ID })), cache: "no-store"
      });
    if (response.status === 409) window.__dshJstBridgeRunning = false;
    if (!response.ok) throw new Error("桥响应 " + response.status);
    return response;
  }
  async function reportSession() {
    await local("/session", { cookie: "", href: location.origin + location.pathname });
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
      if (url.protocol !== "https:" || !["erp321.com"].some(
        host => url.hostname === host || url.hostname.endsWith("." + host))) {
        throw new Error("不支持的桥请求地址");
      }
      const headers = Object.assign({}, task.headers || {});
      const token = document.cookie.split(";").map(x => x.trim()).find(
        x => x.startsWith("XSRF-TOKEN=") || x.startsWith("X-XSRF-TOKEN="));
      if (token) headers["x-xsrf-token"] = decodeURIComponent(token.slice(token.indexOf("=") + 1));
      const response = await fetch(task.url, {
        method: task.method || "GET", headers,
        body: task.body, credentials: "include", redirect: "error",
        cache: "no-store", signal: controller.signal
      });
      const reader = response.body && response.body.getReader();
      if (!reader) throw new Error("响应无可读流");
      const chunks = [];
      let length = 0;
      while (true) {
        const part = await reader.read();
        if (part.done) break;
        length += part.value.byteLength;
        if (length > MAX_BINARY) {
          await reader.cancel();
          throw new Error("响应体超过 24MB，请使用签名地址直连下载");
        }
        chunks.push(part.value);
      }
      const buffer = new Uint8Array(length);
      let offset = 0;
      for (const chunk of chunks) { buffer.set(chunk, offset); offset += chunk.length; }
      const contentType = response.headers.get("content-type") || "";
      const isText = /json|text|javascript|xml/i.test(contentType);
      return { status: response.status, url: response.url, contentType,
        disposition: response.headers.get("content-disposition") || "",
        text: isText ? new TextDecoder("utf-8").decode(buffer) : "",
        base64: isText ? "" : toBase64(new Uint8Array(buffer)) };
    } finally { clearTimeout(timer); }
  }
  async function loop() {
    while (window.__dshJstBridgeRunning) {
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
        await new Promise(resolve => setTimeout(resolve, 2000));
      }
    }
  }
  loop();
  return "bridge agent started";
})();
