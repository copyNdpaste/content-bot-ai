#!/usr/bin/env node

const pagePrefix = process.argv[2] || "https://www.threads.com/";
const needle = process.argv[3] || "삭제";

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

const expression = `(() => {
  const needle = ${JSON.stringify(needle)};
  const textOf = (el) => (el.innerText || el.textContent || "").replace(/\\s+/g, " ").trim();
  return [...document.querySelectorAll("button, [role=button], [role=menuitem], div, span")]
    .map((el, index) => {
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      return {
        index,
        tag: el.tagName,
        role: el.getAttribute("role"),
        aria: el.getAttribute("aria-label"),
        text: textOf(el).slice(0, 260),
        display: style.display,
        visibility: style.visibility,
        opacity: style.opacity,
        pointerEvents: style.pointerEvents,
        rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) },
      };
    })
    .filter((item) => item.text.includes(needle))
    .sort((a, b) => (a.rect.w * a.rect.h) - (b.rect.w * b.rect.h))
    .slice(0, 100);
})()`;

const result = await send("Runtime.evaluate", { expression, returnByValue: true });
console.log(JSON.stringify(result.result?.value ?? result, null, 2));
ws.close();
