#!/usr/bin/env node

import fs from "node:fs/promises";

const args = {
  targetFile: "var/delete-runs/threads-kr-profile-remaining-after-thread1-20260614.json",
  output: "var/delete-runs/threads-kr-profile-delete-textclick-20260614.json",
  start: 0,
  limit: 100,
  delayMs: 5000,
};

for (let i = 2; i < process.argv.length; i += 1) {
  const arg = process.argv[i];
  if (arg === "--target-file") args.targetFile = process.argv[++i];
  else if (arg === "--output") args.output = process.argv[++i];
  else if (arg === "--start") args.start = Number(process.argv[++i]);
  else if (arg === "--limit") args.limit = Number(process.argv[++i]);
  else if (arg === "--delay-ms") args.delayMs = Number(process.argv[++i]);
  else throw new Error(`Unknown argument: ${arg}`);
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

class Cdp {
  constructor(wsUrl) {
    this.ws = new WebSocket(wsUrl);
    this.nextId = 1;
    this.pending = new Map();
    this.ws.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (message.id && this.pending.has(message.id)) {
        const { resolve, reject } = this.pending.get(message.id);
        this.pending.delete(message.id);
        if (message.error) reject(new Error(JSON.stringify(message.error)));
        else resolve(message.result ?? {});
      }
    });
  }
  async open() {
    await new Promise((resolve, reject) => {
      this.ws.addEventListener("open", resolve, { once: true });
      this.ws.addEventListener("error", reject, { once: true });
    });
  }
  send(method, params = {}) {
    const id = this.nextId++;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`CDP timeout: ${method}`));
        }
      }, 45000);
    });
  }
  async eval(expression) {
    const result = await this.send("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
      userGesture: true,
    });
    if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
    return result.result?.value;
  }
  close() {
    this.ws.close();
  }
}

async function findPage() {
  const pages = await (await fetch("http://127.0.0.1:9222/json/list")).json();
  const page = pages.find((item) => item.type === "page" && item.url.startsWith("https://www.threads.com/"));
  if (!page) throw new Error("No Threads page found.");
  return page;
}

async function waitLoaded(cdp) {
  for (let i = 0; i < 30; i += 1) {
    const ready = await cdp.eval(`(() => ({
      state: document.readyState,
      textLength: (document.body?.innerText || "").length,
      buttons: document.querySelectorAll('button, [role="button"]').length,
    }))()`).catch(() => ({ state: "", textLength: 0, buttons: 0 }));
    if (ready.state === "complete" && ready.textLength > 80 && ready.buttons > 0) {
      await sleep(1200);
      return ready;
    }
    await sleep(1000);
  }
  return null;
}

async function navigate(cdp, url) {
  await cdp.send("Page.navigate", { url });
  await waitLoaded(cdp);
  await cdp.eval("window.scrollTo(0, 0); true");
  await sleep(1000);
}

async function snapshot(cdp) {
  return cdp.eval(`(() => ({ href: location.href, title: document.title, text: (document.body?.innerText || "").slice(0, 1600) }))()`);
}

async function openMoreAndClickDelete(cdp) {
  return cdp.eval(`(async () => {
    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    const visible = (el) => {
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };
    const textOf = (el) => (el.innerText || el.textContent || "").replace(/\\s+/g, " ").trim();
    const clickElement = (el) => {
      const rect = el.getBoundingClientRect();
      const target = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2) || el;
      const clickable = target.closest?.('button, [role="button"], [role="menuitem"]') || target.parentElement || el;
      for (const type of ["pointerover", "mouseover", "pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
        const EventCtor = type.startsWith("pointer") ? PointerEvent : MouseEvent;
        clickable.dispatchEvent(new EventCtor(type, {
          bubbles: true,
          cancelable: true,
          view: window,
          clientX: rect.left + rect.width / 2,
          clientY: rect.top + rect.height / 2,
          button: 0,
        }));
      }
      clickable.click?.();
      return { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) };
    };
    const moreCandidates = [...document.querySelectorAll('button, [role="button"]')]
      .filter(visible)
      .filter((el) => textOf(el) === "더 보기" || [...el.querySelectorAll("svg")].some((svg) => svg.getAttribute("aria-label") === "더 보기"))
      .map((el) => ({ el, rect: el.getBoundingClientRect() }));
    const more = moreCandidates.find(({ rect }) => rect.x > 250 && rect.y > 80);
    if (!more) return { ok: false, stage: "find_more", body: (document.body?.innerText || "").slice(0, 800) };
    const moreRect = clickElement(more.el);
    await sleep(1400);
    const deleteCandidates = [...document.querySelectorAll('[role="menuitem"], button, [role="button"], div, span')]
      .filter(visible)
      .filter((el) => textOf(el) === "삭제")
      .sort((a, b) => {
        const ar = a.getBoundingClientRect();
        const br = b.getBoundingClientRect();
        const aRole = a.getAttribute("role") === "menuitem" ? 0 : 1;
        const bRole = b.getAttribute("role") === "menuitem" ? 0 : 1;
        return aRole - bRole || (br.width * br.height) - (ar.width * ar.height);
      });
    const del = deleteCandidates[0];
    if (!del) return { ok: false, stage: "find_delete", moreRect, body: (document.body?.innerText || "").slice(0, 1200) };
    const deleteRect = clickElement(del);
    await sleep(1600);
    return { ok: true, moreRect, deleteRect, body: (document.body?.innerText || "").slice(0, 1200) };
  })()`);
}

async function clickDeleteByMenuPoint(cdp) {
  const point = await cdp.eval(`(() => {
    const visible = (el) => {
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };
    const textOf = (el) => (el.innerText || el.textContent || "").replace(/\\s+/g, " ").trim();
    const menus = [...document.querySelectorAll('[role="menu"], div')]
      .filter(visible)
      .filter((el) => {
        const text = textOf(el);
        return text.includes("답글 옵션") && text.includes("삭제") && text.includes("링크 복사");
      })
      .map((el) => ({ rect: el.getBoundingClientRect(), text: textOf(el) }))
      .sort((a, b) => (a.rect.width * a.rect.height) - (b.rect.width * b.rect.height));
    const match = menus.find(({ rect }) => rect.width >= 180 && rect.height >= 250) || menus.at(-1);
    if (!match) return { ok: false, body: (document.body?.innerText || "").slice(0, 1200) };
    const rect = match.rect;
    return {
      ok: true,
      x: Math.round(rect.left + Math.min(90, rect.width * 0.4)),
      y: Math.round(rect.top + rect.height * 0.78),
      rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) },
    };
  })()`);
  if (!point?.ok) return point;
  for (const type of ["mouseMoved", "mousePressed", "mouseReleased"]) {
    await cdp.send("Input.dispatchMouseEvent", {
      type,
      x: point.x,
      y: point.y,
      button: type === "mouseMoved" ? undefined : "left",
      clickCount: type === "mouseMoved" ? undefined : 1,
    });
  }
  await sleep(1800);
  const after = await snapshot(cdp);
  return { ok: after.text.includes("게시물을 삭제하시겠어요?"), point, after };
}

async function clickConfirmDelete(cdp) {
  const first = await cdp.eval(`(async () => {
    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    const visible = (el) => {
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };
    const textOf = (el) => (el.innerText || el.textContent || "").replace(/\\s+/g, " ").trim();
    const candidates = [...document.querySelectorAll('button, [role="button"], [role="menuitem"], div, span')]
      .filter(visible)
      .filter((el) => textOf(el) === "삭제")
      .sort((a, b) => {
        const ar = a.getBoundingClientRect();
        const br = b.getBoundingClientRect();
        const aRole = a.getAttribute("role") === "button" ? 0 : 1;
        const bRole = b.getAttribute("role") === "button" ? 0 : 1;
        return aRole - bRole || (br.width * br.height) - (ar.width * ar.height);
      });
    const el = candidates[0];
    if (!el) return { ok: false, stage: "find_confirm", body: (document.body?.innerText || "").slice(0, 1200) };
    const rect = el.getBoundingClientRect();
    for (const type of ["pointerover", "mouseover", "pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
      const EventCtor = type.startsWith("pointer") ? PointerEvent : MouseEvent;
      el.dispatchEvent(new EventCtor(type, {
        bubbles: true,
        cancelable: true,
        view: window,
        clientX: rect.left + rect.width / 2,
        clientY: rect.top + rect.height / 2,
        button: 0,
      }));
    }
    el.click?.();
    await sleep(1800);
    return { ok: true, rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) }, body: (document.body?.innerText || "").slice(0, 1200), href: location.href };
  })()`);
  if (!first.ok) return first;
  if (!first.body.includes("게시물을 삭제하시겠어요?")) return first;
  const x = first.rect.x + first.rect.w / 2;
  const y = first.rect.y + first.rect.h / 2;
  for (const type of ["mouseMoved", "mousePressed", "mouseReleased"]) {
    await cdp.send("Input.dispatchMouseEvent", {
      type,
      x,
      y,
      button: type === "mouseMoved" ? undefined : "left",
      clickCount: type === "mouseMoved" ? undefined : 1,
    });
  }
  await sleep(2200);
  const after = await snapshot(cdp);
  return {
    ...first,
    fallbackPoint: { x: Math.round(x), y: Math.round(y) },
    body: after.text,
    href: after.href,
    ok: !after.text.includes("게시물을 삭제하시겠어요?"),
  };
}

async function deleteOne(cdp, target) {
  await navigate(cdp, target.permalink);
  const before = await snapshot(cdp);
  if (before.text.includes("이용할 수 없는 게시물") || before.text.includes("접근할 수 없는 게시물")) {
    return { ok: true, before, skipped: "unavailable" };
  }
  let menu = await openMoreAndClickDelete(cdp);
  if (!menu.ok && menu.stage === "find_delete") {
    const fallback = await clickDeleteByMenuPoint(cdp);
    menu = { ...menu, fallback, ok: Boolean(fallback?.ok) };
  }
  if (!menu.ok) return { ok: false, before, menu };
  const confirm = await clickConfirmDelete(cdp);
  if (!confirm.ok) return { ok: false, before, menu, confirm };
  await sleep(4500);
  await navigate(cdp, target.permalink);
  const verify = await snapshot(cdp);
  const gone =
    verify.href === "https://www.threads.com/" ||
    verify.href === "https://www.threads.com/@onlyfriends_kr_official" ||
    !verify.text.includes("onlyfriends_kr_official") ||
    verify.text.includes("콘텐츠를 사용할 수 없습니다");
  return { ok: gone, before, menu, confirm, verify };
}

const targetsPayload = JSON.parse(await fs.readFile(args.targetFile, "utf8"));
const targets = targetsPayload.items.slice(args.start, args.start + args.limit);
const page = await findPage();
const cdp = new Cdp(page.webSocketDebuggerUrl);
await cdp.open();
await cdp.send("Page.enable");
await cdp.send("Runtime.enable");

const results = [];
async function write() {
  await fs.writeFile(args.output, JSON.stringify({ generatedAt: new Date().toISOString(), args, count: results.length, results }, null, 2));
}

try {
  for (const [offset, target] of targets.entries()) {
    const index = args.start + offset;
    const result = await deleteOne(cdp, target).catch((error) => ({ ok: false, error: error.stack || error.message }));
    results.push({ index, target, result });
    await write();
    if (!result.ok) break;
    await sleep(args.delayMs);
  }
} finally {
  cdp.close();
}

const finalPayload = { generatedAt: new Date().toISOString(), args, count: results.length, results };
await fs.writeFile(args.output, JSON.stringify(finalPayload, null, 2));
console.log(JSON.stringify(finalPayload, null, 2));
