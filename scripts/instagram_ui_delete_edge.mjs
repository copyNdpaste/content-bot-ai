#!/usr/bin/env node

import fs from "node:fs/promises";

const DEFAULT_LIST_URL = "http://127.0.0.1:9222/json/list";
const DEFAULT_TARGET_FILE = "var/delete-runs/instagram-kr-from-20260522-20260614.json";

function parseArgs(argv) {
  const args = {
    targetFile: DEFAULT_TARGET_FILE,
    listUrl: DEFAULT_LIST_URL,
    mode: "inspect",
    limit: 1,
    execute: false,
    start: 0,
    delayMs: 5000,
    output: "",
    pageUrlPrefix: "https://www.instagram.com/",
  };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--target-file") args.targetFile = argv[++i];
    else if (arg === "--list-url") args.listUrl = argv[++i];
    else if (arg === "--mode") args.mode = argv[++i];
    else if (arg === "--limit") args.limit = Number(argv[++i]);
    else if (arg === "--start") args.start = Number(argv[++i]);
    else if (arg === "--delay-ms") args.delayMs = Number(argv[++i]);
    else if (arg === "--output") args.output = argv[++i];
    else if (arg === "--page-url-prefix") args.pageUrlPrefix = argv[++i];
    else if (arg === "--execute") args.execute = true;
    else {
      throw new Error(`Unknown argument: ${arg}`);
    }
  }
  return args;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

class Cdp {
  constructor(wsUrl) {
    this.ws = new WebSocket(wsUrl);
    this.nextId = 1;
    this.pending = new Map();
    this.events = [];
    this.ws.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (message.id && this.pending.has(message.id)) {
        const { resolve, reject } = this.pending.get(message.id);
        this.pending.delete(message.id);
        if (message.error) reject(new Error(JSON.stringify(message.error)));
        else resolve(message.result ?? {});
      } else if (message.method) {
        this.events.push(message);
        if (this.events.length > 1000) this.events.shift();
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
      }, 60000);
    });
  }

  async eval(expression, awaitPromise = true) {
    const result = await this.send("Runtime.evaluate", {
      expression,
      awaitPromise,
      returnByValue: true,
      userGesture: true,
    });
    if (result.exceptionDetails) {
      throw new Error(JSON.stringify(result.exceptionDetails));
    }
    return result.result?.value;
  }

  async close() {
    this.ws.close();
  }
}

async function readTargets(path) {
  const payload = JSON.parse(await fs.readFile(path, "utf8"));
  if (!Array.isArray(payload.items)) {
    throw new Error(`No items array in ${path}`);
  }
  return payload.items.filter((item) => item.permalink);
}

async function findPage(listUrl, pageUrlPrefix) {
  const pages = await (await fetch(listUrl)).json();
  const candidates = pages.filter(
    (page) => page.type === "page" && page.url?.startsWith(pageUrlPrefix),
  );
  if (candidates.length === 0) {
    throw new Error(`No page matching ${pageUrlPrefix} found on Edge remote debugging port.`);
  }
  return candidates[0];
}

async function navigate(cdp, url) {
  await cdp.send("Page.enable");
  await cdp.send("Runtime.enable");
  await cdp.send("Page.navigate", { url });
  for (let attempt = 0; attempt < 24; attempt += 1) {
    await sleep(1000);
    const ready = await cdp
      .eval(`(() => ({
        readyState: document.readyState,
        textLength: (document.body?.innerText || "").length,
        buttonCount: document.querySelectorAll('button, [role="button"]').length,
      }))()`)
      .catch(() => ({ readyState: "", textLength: 0, buttonCount: 0 }));
    if (ready.readyState === "complete" && ready.textLength > 100 && ready.buttonCount > 0) {
      await sleep(1500);
      return;
    }
  }
}

async function snapshot(cdp) {
  return cdp.eval(`(() => {
    const visible = (el) => {
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };
    const textOf = (el) => (el.innerText || el.textContent || "").replace(/\\s+/g, " ").trim();
    return {
      href: location.href,
      title: document.title,
      bodyText: document.body.innerText.slice(0, 1200),
      buttons: [...document.querySelectorAll('button, [role="button"]')]
        .filter(visible)
        .slice(0, 120)
        .map((el, index) => ({
          index,
          tag: el.tagName,
          role: el.getAttribute("role"),
          aria: el.getAttribute("aria-label"),
          title: el.getAttribute("title"),
          text: textOf(el).slice(0, 160),
          rect: (() => {
            const rect = el.getBoundingClientRect();
            return { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) };
          })(),
          svgAria: [...el.querySelectorAll("svg")].map((svg) => svg.getAttribute("aria-label")).filter(Boolean),
        })),
      dialogs: [...document.querySelectorAll('[role="dialog"], div[aria-modal="true"]')]
        .filter(visible)
        .map((el) => textOf(el).slice(0, 800)),
    };
  })()`);
}

async function clickDeleteFlow(cdp) {
  const clickResult = await cdp.eval(`(async () => {
    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    const visible = (el) => {
      if (!el) return false;
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };
    const textOf = (el) => (el.innerText || el.textContent || "").replace(/\\s+/g, " ").trim();
    const click = (el) => {
      const rect = el.getBoundingClientRect();
      if (rect.top < 0 || rect.left < 0 || rect.bottom > window.innerHeight || rect.right > window.innerWidth) {
        el.scrollIntoView({ block: "center", inline: "center" });
      }
      el.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));
      el.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true, view: window }));
      el.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true, view: window }));
      el.click();
    };
    const candidates = () => [...document.querySelectorAll('button, [role="button"]')].filter(visible);
    const textCandidates = () => [...document.querySelectorAll('button, [role="button"], [role="menuitem"], div, span')]
      .filter(visible)
      .map((el) => {
        const rect = el.getBoundingClientRect();
        return { el, text: textOf(el), area: rect.width * rect.height };
      })
      .filter((item) => item.text && item.area > 0)
      .sort((a, b) => a.area - b.area);
    const clickText = (predicate) => {
      const item = textCandidates().find((candidate) => predicate(candidate.text.toLowerCase(), candidate.text));
      if (!item) return false;
      click(item.el.closest('button, [role="button"], [role="menuitem"]') || item.el);
      return true;
    };
    const clickTextNode = (predicate) => {
      const nodes = [];
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      let node;
      while ((node = walker.nextNode())) {
        const text = (node.nodeValue || "").replace(/\\s+/g, " ").trim();
        if (!text || !predicate(text.toLowerCase(), text)) continue;
        let el = node.parentElement;
        for (let depth = 0; el && depth < 6; depth += 1, el = el.parentElement) {
          if (!visible(el)) continue;
          const rect = el.getBoundingClientRect();
          if (rect.width <= 0 || rect.height <= 0) continue;
          nodes.push({ el, area: rect.width * rect.height, rect });
          break;
        }
      }
      nodes.sort((a, b) => a.area - b.area);
      const nodeMatch = nodes[0];
      if (!nodeMatch) return false;
      const rect = nodeMatch.rect;
      const target = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2) || nodeMatch.el;
      click(target.closest('button, [role="button"], [role="menuitem"]') || target);
      return true;
    };
    const moreCandidates = candidates().filter((el) => {
      const aria = (el.getAttribute("aria-label") || "").toLowerCase();
      const title = (el.getAttribute("title") || "").toLowerCase();
      const svgAria = [...el.querySelectorAll("svg")].map((svg) => (svg.getAttribute("aria-label") || "").toLowerCase()).join(" ");
      const text = textOf(el).toLowerCase();
      return aria.includes("more options") || aria.includes("옵션") || title.includes("more options") ||
        svgAria.includes("more options") || svgAria.includes("옵션") || svgAria.includes("더 보기") ||
        text === "더 보기" || text === "•••" || text === "...";
    });
    const more = moreCandidates.find((el) => {
      const rect = el.getBoundingClientRect();
      return textOf(el) === "더 보기" && rect.x > 250 && rect.y > 80;
    }) || moreCandidates.find((el) => {
      const rect = el.getBoundingClientRect();
      return rect.x > 250 && rect.y > 80;
    }) || moreCandidates[0];
    if (!more) {
      return { ok: false, stage: "find_more", buttons: candidates().map((el) => ({
        aria: el.getAttribute("aria-label"),
        text: textOf(el).slice(0, 80),
        rect: (() => {
          const rect = el.getBoundingClientRect();
          return { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) };
        })(),
        svgAria: [...el.querySelectorAll("svg")].map((svg) => svg.getAttribute("aria-label")).filter(Boolean),
      })).slice(0, 80) };
    }
    click(more);
    await sleep(1800);
    const menuButtons = candidates();
    const deleteMenu = menuButtons.find((el) => {
      const text = textOf(el).toLowerCase();
      return text === "삭제" || text === "delete" || text.includes("삭제") || text.includes("delete");
    });
    if (deleteMenu) {
      click(deleteMenu);
    } else if (
      !clickText((lower) => lower.includes("삭제") || lower.includes("delete")) &&
      !clickTextNode((lower) => lower.includes("삭제") || lower.includes("delete"))
    ) {
      return { ok: false, stage: "find_delete_menu", dialog: document.body.innerText.slice(0, 1200) };
    }
    await sleep(1800);
    const confirmButtons = candidates();
    const confirm = confirmButtons.find((el) => {
      const text = textOf(el).toLowerCase();
      return text === "삭제" || text === "delete";
    });
    if (confirm) {
      click(confirm);
    } else if (
      !clickText((lower) => lower.includes("삭제") || lower.includes("delete")) &&
      !clickTextNode((lower) => lower.includes("삭제") || lower.includes("delete"))
    ) {
      return { ok: false, stage: "find_delete_confirm", dialog: document.body.innerText.slice(0, 1200) };
    }
    await sleep(5000);
    return { ok: true, href: location.href, title: document.title, bodyText: document.body.innerText.slice(0, 500) };
  })()`);
  return clickResult;
}

function isUnavailable(snapshotResult) {
  const text = snapshotResult?.bodyText || "";
  return (
    text.includes("페이지를 사용할 수 없습니다") ||
    text.includes("콘텐츠를 사용할 수 없습니다") ||
    text.includes("Sorry, this page isn't available") ||
    text.includes("This content isn't available") ||
    text.includes("link you followed may be broken") ||
    text.includes("페이지가 삭제되었습니다")
  );
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const targets = (await readTargets(args.targetFile)).slice(args.start, args.start + args.limit);
  const page = await findPage(args.listUrl, args.pageUrlPrefix);
  const cdp = new Cdp(page.webSocketDebuggerUrl);
  await cdp.open();

  const results = [];
  const writeOutput = async () => {
    if (!args.output) return;
    await fs.writeFile(
      args.output,
      JSON.stringify({ generatedAt: new Date().toISOString(), args, count: results.length, results }, null, 2),
    );
  };
  try {
    for (const [offset, target] of targets.entries()) {
      const index = args.start + offset;
      try {
        await navigate(cdp, target.permalink);
        const before = await snapshot(cdp);
        if (args.mode === "inspect" || !args.execute) {
          results.push({ index, target, inspected: before });
          await writeOutput();
        } else if (args.mode === "delete") {
          if (isUnavailable(before)) {
            results.push({ index, target, before, deleted: { ok: true, skipped: "already_unavailable" } });
            await writeOutput();
            await sleep(args.delayMs);
            continue;
          }
          const deleted = await clickDeleteFlow(cdp);
          const after = await snapshot(cdp).catch((error) => ({ error: error.message }));
          results.push({ index, target, before, deleted, after });
          await writeOutput();
          if (!deleted.ok) break;
          await sleep(args.delayMs);
        } else {
          throw new Error(`Unknown mode: ${args.mode}`);
        }
      } catch (error) {
        results.push({ index, target, error: error.stack || error.message });
        await writeOutput();
        break;
      }
    }
  } finally {
    await cdp.close();
  }

  const finalPayload = { generatedAt: new Date().toISOString(), args, count: results.length, results };
  if (args.output) {
    await fs.writeFile(args.output, JSON.stringify(finalPayload, null, 2));
  }
  console.log(JSON.stringify(finalPayload, null, 2));
}

main().catch((error) => {
  console.error(error.stack || error.message);
  process.exit(1);
});
