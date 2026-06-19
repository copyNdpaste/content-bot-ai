#!/usr/bin/env node

const pagePrefix = process.argv[2] || "https://www.threads.com/";
const x = Number(process.argv[3]);
const y = Number(process.argv[4]);
if (!Number.isFinite(x) || !Number.isFinite(y)) {
  throw new Error("Usage: node scripts/cdp_click_point.mjs <page-prefix> <x> <y>");
}

const pages = await (await fetch("http://127.0.0.1:9222/json/list")).json();
const page = pages.find((item) => item.type === "page" && item.url.startsWith(pagePrefix));
if (!page) throw new Error(`No page matching ${pagePrefix}`);

const ws = new WebSocket(page.webSocketDebuggerUrl);
const pending = new Map();
let nextId = 1;
ws.addEventListener("message", (event) => {
  const message = JSON.parse(event.data);
  if (message.id && pending.has(message.id)) {
    pending.get(message.id)(message);
    pending.delete(message.id);
  }
});
await new Promise((resolve, reject) => {
  ws.addEventListener("open", resolve, { once: true });
  ws.addEventListener("error", reject, { once: true });
});
const send = (method, params = {}) =>
  new Promise((resolve) => {
    const id = nextId++;
    pending.set(id, resolve);
    ws.send(JSON.stringify({ id, method, params }));
  });

await send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
await send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 });
await send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 });
await new Promise((resolve) => setTimeout(resolve, 1200));
const result = await send("Runtime.evaluate", {
  expression: `(() => ({
    href: location.href,
    title: document.title,
    text: (document.body.innerText || "").slice(0, 1600)
  }))()`,
  returnByValue: true,
});
console.log(JSON.stringify(result.result?.value ?? result, null, 2));
ws.close();
