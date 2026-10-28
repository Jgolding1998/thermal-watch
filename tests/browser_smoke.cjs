const assert = require("node:assert/strict");
const { chromium } = require("playwright");
const path = require("node:path");

(async () => {
  const browser = await chromium.launch({headless:true, channel:process.env.THERMAL_BROWSER_CHANNEL || undefined});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1100}, deviceScaleFactor:1});
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(process.env.THERMAL_TEST_URL || "http://127.0.0.1:8766");
    await page.waitForFunction(() => document.querySelectorAll(".sensor-card").length === 2);
    assert.equal(await page.locator("#mode-badge").textContent(), "DEMO MODE");
    assert.equal(await page.locator("#connection-error").isVisible(), false);
    await page.locator("#unit-f").click();
    assert.equal(await page.locator(".temp-unit").first().textContent(), "°F");
    await page.locator("#unit-c").click();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.screenshot({path:path.join(__dirname,"../docs/dashboard.png"),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.screenshot({path:path.join(__dirname,"../docs/mobile.png"),fullPage:true});
    await page.route("**/api/state", route => route.abort());
    await page.waitForFunction(() => !document.querySelector("#connection-error").hidden, {timeout:7000});
    assert.equal(await page.locator("#connection").textContent(), "Connection lost");
    assert.deepEqual(errors, []);
    console.log("Browser checks passed: desktop/mobile overflow, unit conversion, demo label, lost-connection warning, no script errors.");
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode=1;});
