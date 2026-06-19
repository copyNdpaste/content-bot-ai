#!/usr/bin/env node

const pagePrefix = process.argv[2] || "https://www.threads.com/";
const text = process.argv[3] || "삭제";
const shouldClickMore = process.argv.includes("--click-more");

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

if (shouldClickMore) {
  const clickMoreExpression = `(() => {
    const visible = (el) => {
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };
    const textOf = (el) => (el.innerText || el.textContent || "").replace(/\\s+/g, " ").trim();
    const click = (el) => {
      const rect = el.getBoundingClientRect();
      const target = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2) || el;
      const clickable = target.closest?.('button, [role="button"], [role="menuitem"]') || target.parentElement || el;
      clickable.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));
      clickable.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true, view: window }));
      clickable.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true, view: window }));
      clickable.click();
      return { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) };
    };
    const candidates = [...document.querySelectorAll('button, [role="button"]')]
      .filter(visible)
      .filter((el) => textOf(el) === "더 보기" || [...el.querySelectorAll("svg")].some((svg) => svg.getAttribute("aria-label") === "더 보기"))
      .map((el) => ({ el, rect: el.getBoundingClientRect() }));
    const match = candidates.find(({ rect }) => rect.x > 250 && rect.y > 80) || candidates[0];
    if (!match) return { ok: false, candidates: candidates.length };
    return { ok: true, rect: click(match.el) };
  })()`;
  await send("Runtime.evaluate", { expression: clickMoreExpression, returnByValue: true, userGesture: true });
  await new Promise((resolve) => setTimeout(resolve, 1500));
}

const expression = `(() => {
  const wanted = ${JSON.stringify(text)};
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
  };
  const textOf = (el) => (el.innerText || el.textContent || "").replace(/\\s+/g, " ").trim();
  const matches = [...document.querySelectorAll('[role="menuitem"], button, [role="button"], div, span')]
    .filter(visible)
    .filter((el) => textOf(el) === wanted)
    .sort((a, b) => {
      const ar = a.getBoundingClientRect();
      const br = b.getBoundingClientRect();
      const aRole = a.getAttribute("role") === "menuitem" ? 0 : 1;
      const bRole = b.getAttribute("role") === "menuitem" ? 0 : 1;
      return aRole - bRole || (br.width * br.height) - (ar.width * ar.height);
    });
  const el = matches[0];
  if (!el) return { ok: false, text: document.body.innerText.slice(0, 1000) };
  const rect = el.getBoundingClientRect();
  for (const type of ["pointerover", "pointerenter", "mouseover", "mouseenter", "pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
    const EventCtor = type.startsWith("pointer") ? PointerEvent : MouseEvent;
    el.dispatchEvent(new EventCtor(type, { bubbles: true, cancelable: true, view: window, clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2, button: 0 }));
  }
  el.click?.();
  return {
    ok: true,
    clicked: { tag: el.tagName, role: el.getAttribute("role"), text: textOf(el), rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) } },
    body: document.body.innerText.slice(0, 1400),
  };
})()`;

const result = await send("Runtime.evaluate", {
  expression,
  returnByValue: true,
  userGesture: true,
});
await new Promise((resolve) => setTimeout(resolve, 1500));
const snapshot = await send("Runtime.evaluate", {
  expression: `(() => ({ href: location.href, title: document.title, text: document.body.innerText.slice(0, 1600) }))()`,
  returnByValue: true,
});
console.log(JSON.stringify({ click: result.result?.value ?? result, snapshot: snapshot.result?.value ?? snapshot }, null, 2));
ws.close();
