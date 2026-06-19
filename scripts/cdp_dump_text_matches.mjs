#!/usr/bin/env node

const pagePrefix = process.argv[2] || "https://www.threads.com/";
const needle = process.argv[3] || "삭제";
const shouldClickMore = process.argv.includes("--click-more");

const pages = await (await fetch("http://127.0.0.1:9222/json/list")).json();
const page = pages.find((item) => item.type === "page" && item.url.startsWith(pagePrefix));
if (!page) {
  throw new Error(`No page matching ${pagePrefix}`);
}

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
  const clickExpression = `(() => {
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
      target.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));
      target.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true, view: window }));
      target.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true, view: window }));
      clickable.click();
    };
    const candidates = [...document.querySelectorAll('button, [role="button"]')]
      .filter(visible)
      .filter((el) => textOf(el) === "더 보기" || [...el.querySelectorAll("svg")].some((svg) => svg.getAttribute("aria-label") === "더 보기"))
      .map((el) => ({ el, rect: el.getBoundingClientRect(), text: textOf(el) }));
    const match = candidates.find(({ rect }) => rect.x > 250 && rect.y > 80) || candidates[0];
    if (!match) return { ok: false, candidates: candidates.length };
    click(match.el);
    return { ok: true, rect: { x: Math.round(match.rect.x), y: Math.round(match.rect.y), w: Math.round(match.rect.width), h: Math.round(match.rect.height) } };
  })()`;
  const clicked = await send("Runtime.evaluate", {
    expression: clickExpression,
    returnByValue: true,
    userGesture: true,
  });
  await new Promise((resolve) => setTimeout(resolve, 1500));
  console.error(JSON.stringify(clicked.result?.value ?? clicked));
}

const expression = `(() => {
  const needle = ${JSON.stringify(needle)};
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
  };
  const textOf = (el) => (el.innerText || el.textContent || "").replace(/\\s+/g, " ").trim();
  const elements = [...document.querySelectorAll("button, [role=button], [role=menuitem], div, span")];
  return elements
    .map((el, index) => {
      const rect = el.getBoundingClientRect();
      return {
        index,
        tag: el.tagName,
        role: el.getAttribute("role"),
        aria: el.getAttribute("aria-label"),
        text: textOf(el).slice(0, 200),
        visible: visible(el),
        area: Math.round(rect.width * rect.height),
        rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) },
      };
    })
    .filter((item) => item.visible && item.text.includes(needle))
    .sort((a, b) => a.area - b.area)
    .slice(0, 80);
})()`;

const result = await send("Runtime.evaluate", {
  expression,
  returnByValue: true,
});
console.log(JSON.stringify(result.result?.value ?? result, null, 2));
ws.close();
