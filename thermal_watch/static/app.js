"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
let unit = "C";
let lastState = null;
const cards = new Map();
const statusLabels = {normal: "In range", pending: "Checking excursion", low: "Too cold", high: "Too hot", stale: "Sensor offline", fault: "Input fault", waiting: "Awaiting reading"};
const attention = new Set(["low", "high", "stale", "fault"]);
const format = value => value === null ? "..." : (unit === "F" ? value * 9 / 5 + 32 : value).toFixed(1);
const text = (selector, value, root = document) => { $(selector, root).textContent = value; };

function chart(card, history, minimum, maximum) {
  const values = history.map(point => point.value_c).filter(Number.isFinite);
  if (!values.length) return;
  const low = Math.min(minimum, ...values) - 2;
  const high = Math.max(maximum, ...values) + 2;
  const points = values.map((value, i) => [values.length === 1 ? 480 : i / (values.length - 1) * 480, 78 - (value - low) / (high - low) * 60]);
  if (points.length === 1) points.unshift([0, points[0][1]]);
  const path = points.map(([x,y], i) => `${i ? "L" : "M"}${x.toFixed(2)},${y.toFixed(2)}`).join(" ");
  $(".chart-line", card).setAttribute("d", path);
  $(".chart-area", card).setAttribute("d", `${path} L480,88 L0,88 Z`);
}

function renderSensor(sensor) {
  let card = cards.get(sensor.id);
  if (!card) {
    card = $("#sensor-template").content.firstElementChild.cloneNode(true);
    cards.set(sensor.id, card);
    $("#sensor-grid").append(card);
  }
  card.dataset.status = sensor.status;
  text(".sensor-name", sensor.label, card);
  text(".sensor-id", sensor.id, card);
  text(".sensor-status", statusLabels[sensor.status] || "Unknown", card);
  text(".temperature strong", format(sensor.value_c), card);
  text(".temp-unit", `°${unit}`, card);
  text(".sensor-range", `${format(sensor.minimum_c)} to ${format(sensor.maximum_c)} °${unit}`, card);
  text(".sensor-age", sensor.age_seconds === null ? "No reading yet" : `${Math.floor(sensor.age_seconds)}s since reading`, card);
  let message = "Looking good. Temperature is inside its target range.";
  if (sensor.status === "pending") message = "Outside the range. Waiting for the configured dwell time.";
  if (sensor.status === "high") message = "Above the upper limit. An alert has been recorded.";
  if (sensor.status === "low") message = "Below the lower limit. An alert has been recorded.";
  if (sensor.status === "stale") message = "No fresh data. The displayed reading is historical.";
  if (sensor.status === "fault") message = "Invalid sensor input. Check the producer before relying on readings.";
  if (sensor.status === "waiting") message = "Waiting for the first reading from this sensor.";
  text(".sensor-message", message, card);
  chart(card, sensor.history, sensor.minimum_c, sensor.maximum_c);
}

function renderEvents(events) {
  text("#event-count", events.length);
  if (!events.length) return;
  const list = $("#event-list");
  list.replaceChildren();
  for (const event of events) {
    const row = document.createElement("div"); row.className = "event"; row.dataset.kind = event.kind;
    const icon = document.createElement("span"); icon.className = "event-icon"; icon.textContent = ["recovered", "online"].includes(event.kind) ? "✓" : "!";
    const copy = document.createElement("div"); copy.className = "event-copy";
    const title = document.createElement("strong"); title.textContent = event.message;
    const detail = document.createElement("p"); detail.textContent = `${event.sensor} · ${event.delivery === "sent" ? "Webhook accepted" : event.delivery === "failed" ? "Webhook failed; saved locally" : "Saved in local log"}`;
    const at = document.createElement("time"); const date = new Date(event.created * 1000); at.dateTime = date.toISOString(); at.textContent = date.toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"});
    copy.append(title, detail); row.append(icon, copy, at); list.append(row);
  }
}

function render(state) {
  const isDemo = state.mode === "demo";
  text("#mode-title", isDemo ? "You are exploring the demo" : "Connected to your local sensor feed");
  text("#mode-copy", isDemo ? "Simulated readings cycle through a warm freezer and a cool cooker every 90 seconds. No real sensors or outbound alerts are connected." : "Readings arrive from your CSV producer. Missing or invalid data is shown as a sensor problem.");
  text("#mode-badge", isDemo ? "DEMO MODE" : "LIVE INPUT");
  text("#sensor-total", state.sensors.length);
  const healthy = state.sensors.filter(sensor => sensor.status === "normal").length;
  text("#normal-total", healthy);
  text("#normal-caption", healthy === state.sensors.length ? "All monitored temperatures look good" : "Sensors within the configured range");
  text("#attention-total", state.sensors.filter(sensor => attention.has(sensor.status)).length);
  text("#delivery", state.webhook_enabled ? "Webhook" : "Local log");
  text("#delivery-caption", state.webhook_enabled ? "Your endpoint receives new alerts" : "No external endpoint connected");
  text("#updated", `Updated ${new Date(state.now * 1000).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit",second:"2-digit"})}`);
  $("#input-error").hidden = !state.input_error;
  text("#input-error", state.input_error || "");
  state.sensors.forEach(renderSensor);
  renderEvents(state.events);
}

async function update() {
  try {
    const response = await fetch("/api/state", {cache:"no-store", signal:AbortSignal.timeout(4000)});
    if (!response.ok) throw new Error("Unavailable");
    const state = await response.json();
    lastState = state;
    render(state);
    text("#connection", "Service connected");
    $("#connection").classList.remove("lost");
    $("#connection-error").hidden = true;
  } catch (_) {
    text("#connection", "Connection lost");
    $("#connection").classList.add("lost");
    $("#connection-error").hidden = false;
  }
  window.setTimeout(update, 2000);
}

for (const next of ["C", "F"]) {
  $(`#unit-${next.toLowerCase()}`).addEventListener("click", () => {
    unit = next;
    for (const candidate of ["C", "F"]) {
      const button = $(`#unit-${candidate.toLowerCase()}`);
      button.classList.toggle("selected", candidate === next);
      button.setAttribute("aria-pressed", String(candidate === next));
    }
    if (lastState) render(lastState);
  });
}
update();
