// Load the built demo in Chromium, click through every preset and check the
// lineage panel fills in. Usage: node site/test/smoke.mjs http://localhost:8000/
import { chromium } from "playwright";

const url = process.argv[2];
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));

const ready = () =>
  page.waitForFunction(
    () => /with lineage|error/i.test(document.querySelector("#status").textContent) && !document.querySelector("#run").disabled,
    null,
    { timeout: 120000 },
  );

const t0 = Date.now();
await page.goto(url);
await ready();
console.log(`first chart after ${((Date.now() - t0) / 1000).toFixed(1)} s`);

let failed = false;
for (const id of ["days", "longest", "payment", "refunds"]) {
  await page.click(`#tabs button[data-id="${id}"]`);
  await ready();
  const status = await page.textContent("#status");
  await page.click('.hit[data-i="0"]');
  await page.waitForSelector(".source table tbody tr", { timeout: 60000 });
  const panel = await page.textContent("#panel .source h3");
  console.log(`${id.padEnd(8)} ${status} | ${panel.trim()}`);
  if (!/with lineage/.test(status)) failed = true;
}
if (errors.length) {
  console.log("page errors:", errors);
  failed = true;
}
await browser.close();
process.exit(failed ? 1 : 0);
