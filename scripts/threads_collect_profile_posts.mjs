#!/usr/bin/env node

import fs from "node:fs/promises";

const profileUrl = process.argv[2] || "https://www.threads.com/@onlyfriends_kr_official";
const output = process.argv[3] || "var/delete-runs/threads-kr-profile-remaining-20260614.json";
const maxScrolls = Number(process.argv[4] || 20);

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
      }, 30000);
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

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function findPage() {
  const pages = await (await fetch("http://127.0.0.1:9222/json/list")).json();
  const page = pages.find((item) => item.type === "page" && item.url.startsWith("https://www.threads.com/"));
  if (!page) throw new Error("No Threads page found on CDP port.");
  return page;
}

const page = await findPage();
const cdp = new Cdp(page.webSocketDebuggerUrl);
await cdp.open();
await cdp.send("Page.enable");
await cdp.send("Runtime.enable");
await cdp.send("Page.navigate", { url: profileUrl });
await sleep(6000);

const seen = new Map();
let stableRounds = 0;
let lastCount = 0;

for (let i = 0; i < maxScrolls; i += 1) {
  const links = await cdp.eval(`(() => {
    const textNear = (el) => {
      let node = el;
      for (let i = 0; node && i < 8; i += 1, node = node.parentElement) {
        const text = (node.innerText || node.textContent || "").replace(/\\s+/g, " ").trim();
        if (text.length > 20) return text.slice(0, 240);
      }
      return "";
    };
    const hrefs = [...document.querySelectorAll('a[href*="/post/"]')]
      .map((a) => ({ href: new URL(a.getAttribute("href"), location.href).href, text: textNear(a) }))
      .filter((item) => item.href.includes("/@onlyfriends_kr_official/post/"));
    return hrefs;
  })()`);
  for (const item of links) {
    if (!seen.has(item.href)) seen.set(item.href, item);
  }
  if (seen.size === lastCount) stableRounds += 1;
  else stableRounds = 0;
  lastCount = seen.size;
  if (stableRounds >= 4) break;
  await cdp.eval("window.scrollBy(0, Math.floor(window.innerHeight * 0.85)); true");
  await sleep(2200);
}

const items = [...seen.values()].map((item, index) => ({
  platform: "threads",
  account: "kr",
  index,
  permalink: item.href.split("?")[0],
  preview: item.text,
}));

const payload = {
  generatedAt: new Date().toISOString(),
  profileUrl,
  count: items.length,
  items,
};
await fs.writeFile(output, JSON.stringify(payload, null, 2));
console.log(JSON.stringify(payload, null, 2));
cdp.close();
