#!/usr/bin/env node

import fs from "node:fs/promises";

const pagePrefix = process.argv[2] || "https://www.threads.com/";
const output = process.argv[3] || "var/delete-runs/cdp-screenshot.png";

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

await send("Page.enable");
const result = await send("Page.captureScreenshot", {
  format: "png",
  fromSurface: true,
  captureBeyondViewport: false,
});
const data = result.result?.data ?? result.data;
if (!data) throw new Error(`Missing screenshot data: ${JSON.stringify(result).slice(0, 500)}`);
await fs.writeFile(output, Buffer.from(data, "base64"));
console.log(output);
ws.close();
