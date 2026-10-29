const assert = require("node:assert/strict");
const { chromium } = require("playwright");
const path = require("node:path");
const fs = require("node:fs");

(async () => {
  const browser = await chromium.launch({headless:true, channel:process.env.THERMAL_BROWSER_CHANNEL || undefined});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1100}, deviceScaleFactor:1});
    const errors = [];
    const screenshots = process.env.THERMAL_SCREENSHOT_DIR || path.join(__dirname,"../docs");
    fs.mkdirSync(screenshots, {recursive:true});
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(process.env.THERMAL_TEST_URL || "http://127.0.0.1:8766");
    await page.waitForFunction(() => document.querySelectorAll(".sensor-card").length === 2);
    assert.equal(await page.locator("#mode-badge").textContent(), "DEMO MODE");
    assert.equal(await page.locator("#connection-error").isVisible(), false);
    await page.locator("#unit-f").click();
    assert.equal(await page.locator(".temp-unit").first().textContent(), "°F");
    await page.locator("#unit-c").click();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.screenshot({path:path.join(screenshots,"dashboard.png"),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.screenshot({path:path.join(screenshots,"mobile.png"),fullPage:true});
    const original = await (await page.request.get(new URL("/api/state", page.url()).href)).json();
    let snapshot = structuredClone(original);
    snapshot.sensors[0].label = '<img src=x onerror="globalThis.injected=1">';
    snapshot.events = [{id:1, sensor:snapshot.sensors[0].id, kind:"high", created:snapshot.now, message:snapshot.sensors[0].label, delivery:"local"}];
    await page.route("**/api/state", route => route.fulfill({json:snapshot}));
    await page.waitForFunction(() => document.querySelectorAll("#event-list .event").length === 1);
    assert.equal(await page.locator("#sensor-grid img, #event-list img").count(), 0);
    assert.equal(await page.evaluate(() => globalThis.injected === 1), false);
    snapshot.sensors = snapshot.sensors.slice(0,1);
    snapshot.sensors[0].history = [];
    snapshot.sensors[0].value_c = null;
    snapshot.sensors[0].status = "waiting";
    await page.waitForFunction(() => document.querySelectorAll(".sensor-card").length === 1);
    assert.equal(await page.locator(".chart-line").getAttribute("d"), null);
    assert.equal(await page.locator(".chart-area").getAttribute("d"), null);
    snapshot.sensors = [];
    snapshot.events = [];
    await page.waitForFunction(() => document.querySelectorAll(".sensor-card").length === 0);
    assert.equal(await page.locator("#event-list .event").count(), 0);
    assert.equal(await page.locator("#event-list .empty-state").isVisible(), true);
    assert.equal(await page.locator("#sensor-total").textContent(), "0");
    assert.equal(await page.locator("#event-count").textContent(), "0");
    snapshot = original;
    await page.waitForFunction(() => document.querySelectorAll(".sensor-card").length === 2);
    await page.unroute("**/api/state");
    await page.route("**/api/state", route => route.abort());
    await page.waitForFunction(() => !document.querySelector("#connection-error").hidden, {timeout:7000});
    assert.equal(await page.locator("#connection").textContent(), "Connection lost");
    assert.deepEqual(errors, []);
    console.log("Browser checks passed: desktop/mobile layout, units, demo label, safe text, snapshot replacement, empty charts, connection loss, no script errors.");
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode=1;});
