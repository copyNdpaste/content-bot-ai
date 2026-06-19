#!/usr/bin/env node

import fs from "node:fs/promises";

const args = {
  targetFile: "var/delete-runs/threads-kr-profile-remaining-20260614.json",
  output: "var/delete-runs/threads-kr-profile-delete-mouse-20260614.json",
  start: 0,
  limit: 1,
  delayMs: 7000,
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
    if (result.exceptionDetails) {
      throw new Error(JSON.stringify(result.exceptionDetails));
    }
    return result.result?.value;
  }

  close() {
    this.ws.close();
  }
}

async function findThreadsPage() {
  const pages = await (await fetch("http://127.0.0.1:9222/json/list")).json();
  const page = pages.find((item) => item.type === "page" && item.url.startsWith("https://www.threads.com/"));
  if (!page) throw new Error("No Threads page found on CDP port.");
  return page;
}

async function mouseClick(cdp, rect) {
  const x = Math.round(rect.x + rect.w / 2);
  const y = Math.round(rect.y + rect.h / 2);
  await cdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
  await cdp.send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 });
  await cdp.send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 });
}

async function waitLoaded(cdp) {
  for (let i = 0; i < 30; i += 1) {
    const ready = await cdp.eval(`(() => ({
      readyState: document.readyState,
      textLength: (document.body?.innerText || "").length,
      buttons: document.querySelectorAll('button, [role="button"]').length,
    }))()`).catch(() => ({ readyState: "", textLength: 0, buttons: 0 }));
    if (ready.readyState === "complete" && ready.textLength > 100 && ready.buttons > 0) {
      await sleep(1500);
      return ready;
    }
    await sleep(1000);
  }
  return null;
}

async function navigate(cdp, url) {
  await cdp.send("Page.navigate", { url });
  await waitLoaded(cdp);
}

async function snapshot(cdp) {
  return cdp.eval(`(() => ({
    href: location.href,
    title: document.title,
    text: (document.body?.innerText || "").slice(0, 1200),
  }))()`);
}

async function findMoreRect(cdp) {
  return cdp.eval(`(() => {
    const visible = (el) => {
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };
    const textOf = (el) => (el.innerText || el.textContent || "").replace(/\\s+/g, " ").trim();
    const candidates = [...document.querySelectorAll('button, [role="button"]')]
      .filter(visible)
      .filter((el) => textOf(el) === "더 보기" || [...el.querySelectorAll("svg")].some((svg) => svg.getAttribute("aria-label") === "더 보기"))
      .map((el) => {
        const rect = el.getBoundingClientRect();
        return { text: textOf(el), rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) } };
      });
    return candidates.find((item) => item.rect.x > 250 && item.rect.y > 80) || null;
  })()`);
}

async function openMoreMenu(cdp) {
  return cdp.eval(`(() => {
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
    const match = candidates.find(({ rect }) => rect.x > 250 && rect.y > 80) || null;
    if (!match) return null;
    return click(match.el);
  })()`);
}

async function findDeleteRect(cdp) {
  return cdp.eval(`(() => {
    const visible = (el) => {
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };
    const textOf = (el) => (el.innerText || el.textContent || "").replace(/\\s+/g, " ").trim();
    const items = [...document.querySelectorAll('[role="menuitem"], button, [role="button"], div, span')]
      .filter(visible)
      .map((el) => {
        const rect = el.getBoundingClientRect();
        return {
          text: textOf(el),
          role: el.getAttribute("role"),
          area: rect.width * rect.height,
          rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) },
        };
      })
      .filter((item) => item.text === "삭제" || item.text === "Delete")
      .sort((a, b) => {
        const roleScore = (b.role === "menuitem" ? 1 : 0) - (a.role === "menuitem" ? 1 : 0);
        return roleScore || b.area - a.area;
      });
    if (items[0]) return items[0];
    const menu = [...document.querySelectorAll('[role="menu"], div')]
      .filter(visible)
      .map((el) => {
        const rect = el.getBoundingClientRect();
        return {
          text: textOf(el),
          role: el.getAttribute("role"),
          area: rect.width * rect.height,
          rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) },
        };
      })
      .filter((item) => item.text.includes("답글 옵션") && item.text.includes("삭제") && item.text.includes("링크 복사"))
      .sort((a, b) => a.area - b.area)[0];
    if (menu) {
      return {
        text: "삭제",
        role: "menuitem-fallback",
        rect: { x: menu.rect.x, y: menu.rect.y + Math.round(menu.rect.h * 0.72), w: menu.rect.w, h: 40 },
      };
    }
    return null;
  })()`);
}

async function clickTextDom(cdp, wanted) {
  return cdp.eval(`(async () => {
    const wanted = ${JSON.stringify(wanted)};
    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    const visible = (el) => {
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };
    const textOf = (el) => (el.innerText || el.textContent || "").replace(/\\s+/g, " ").trim();
    const click = (el) => {
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
    };
    const candidates = [...document.querySelectorAll('[role="menuitem"], button, [role="button"], div, span')]
      .filter(visible)
      .filter((el) => textOf(el) === wanted)
      .sort((a, b) => {
        const ar = a.getBoundingClientRect();
        const br = b.getBoundingClientRect();
        const aRole = a.getAttribute("role") === "menuitem" ? 0 : 1;
        const bRole = b.getAttribute("role") === "menuitem" ? 0 : 1;
        return aRole - bRole || (br.width * br.height) - (ar.width * ar.height);
      });
    const el = candidates[0];
    if (!el) return { ok: false, text: document.body.innerText.slice(0, 1000) };
    click(el);
    await sleep(1200);
    return {
      ok: true,
      href: location.href,
      text: document.body.innerText.slice(0, 1200),
      clicked: {
        tag: el.tagName,
        role: el.getAttribute("role"),
        text: textOf(el),
      },
    };
  })()`);
}

async function isGone(cdp, target) {
  await navigate(cdp, target.permalink);
  const snap = await snapshot(cdp);
  const text = snap.text || "";
  return {
    gone:
      snap.href === "https://www.threads.com/" ||
      snap.href === "https://www.threads.com/@onlyfriends_kr_official" ||
      text.includes("콘텐츠를 사용할 수 없습니다") ||
      text.includes("This content isn't available") ||
      !text.includes("onlyfriends_kr_official"),
    snap,
  };
}

async function deleteOne(cdp, target) {
  await navigate(cdp, target.permalink);
  const before = await snapshot(cdp);
  const more = await findMoreRect(cdp);
  if (!more) return { ok: false, stage: "find_more", before };
  await openMoreMenu(cdp);
  await sleep(1200);
  const deleteClicked = await clickTextDom(cdp, "삭제");
  if (!deleteClicked.ok) return { ok: false, stage: "find_delete_menu", before, menuText: (await snapshot(cdp)).text };
  await sleep(1600);
  const confirmClicked = await clickTextDom(cdp, "삭제");
  if (!confirmClicked.ok) return { ok: false, stage: "find_delete_confirm", before, afterDeleteClick: await snapshot(cdp), deleteClicked };
  await sleep(4500);
  const verify = await isGone(cdp, target);
  return { ok: verify.gone, before, verify };
}

const payload = JSON.parse(await fs.readFile(args.targetFile, "utf8"));
const targets = payload.items.slice(args.start, args.start + args.limit);
const page = await findThreadsPage();
const cdp = new Cdp(page.webSocketDebuggerUrl);
await cdp.open();
await cdp.send("Page.enable");
await cdp.send("Runtime.enable");

const results = [];
async function write() {
  await fs.writeFile(
    args.output,
    JSON.stringify({ generatedAt: new Date().toISOString(), args, count: results.length, results }, null, 2),
  );
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
