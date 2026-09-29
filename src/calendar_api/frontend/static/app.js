"use strict";

const MAX_BAND_LANES = 3;
const MAX_TIMED_PER_DAY = 3;
const DAY_MS = 86_400_000;
const PALETTE = [
  { solid: "#447d68", soft: "#dceae4", ink: "#173d31" },
  { solid: "#d8674b", soft: "#fae4dc", ink: "#74301f" },
  { solid: "#4f8097", soft: "#dce9ef", ink: "#244b5d" },
  { solid: "#b98420", soft: "#f4e7c7", ink: "#624711" },
  { solid: "#7c6ca6", soft: "#e9e3f2", ink: "#493b70" },
];

const monthGrid = document.querySelector("#month-grid");
const calendarFrame = document.querySelector(".calendar-frame");
const monthLabel = document.querySelector("#month-label");
const statusRegion = document.querySelector("#status-region");
const monthJump = document.querySelector("#month-jump");
const dayDialog = document.querySelector("#day-dialog");
const eventDialog = document.querySelector("#event-dialog");

const now = new Date();
const state = {
  displayedMonth: new Date(Date.UTC(now.getFullYear(), now.getMonth(), 1)),
  events: [],
  timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
  request: null,
  jumpYear: now.getFullYear(),
};

function utcDate(year, month, day) {
  return new Date(Date.UTC(year, month, day));
}

function addDays(value, days) {
  return new Date(value.getTime() + days * DAY_MS);
}

function dateKey(value) {
  const year = value.getUTCFullYear();
  const month = String(value.getUTCMonth() + 1).padStart(2, "0");
  const day = String(value.getUTCDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function dateFromKey(value) {
  const [year, month, day] = value.split("-").map(Number);
  return utcDate(year, month - 1, day);
}

function compareKeys(left, right) {
  return left.localeCompare(right);
}

function visibleWeeks(monthDate) {
  const first = utcDate(monthDate.getUTCFullYear(), monthDate.getUTCMonth(), 1);
  const last = utcDate(monthDate.getUTCFullYear(), monthDate.getUTCMonth() + 1, 0);
  const gridStart = addDays(first, -first.getUTCDay());
  const gridEnd = addDays(last, 6 - last.getUTCDay());
  const weeks = [];

  for (let cursor = gridStart; cursor <= gridEnd; cursor = addDays(cursor, 7)) {
    weeks.push(Array.from({ length: 7 }, (_, index) => addDays(cursor, index)));
  }
  return weeks;
}

function localDateKey(value) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: state.timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(value);
  const pick = (type) => parts.find((part) => part.type === type).value;
  return `${pick("year")}-${pick("month")}-${pick("day")}`;
}

function eventBounds(event) {
  if (event.is_all_day) {
    return {
      start: event.start_date,
      end: dateKey(addDays(dateFromKey(event.end_date), -1)),
    };
  }
  const start = new Date(event.starts_at);
  const end = new Date(event.ends_at);
  return {
    start: localDateKey(start),
    // Endpoints are exclusive; midnight belongs to the preceding day.
    end: localDateKey(new Date(Math.max(start.getTime(), end.getTime() - 1))),
  };
}

function isBandEvent(event) {
  const bounds = eventBounds(event);
  return event.is_all_day || bounds.start !== bounds.end;
}

function overlapsDay(event, key) {
  const bounds = eventBounds(event);
  return compareKeys(bounds.start, key) <= 0 && compareKeys(bounds.end, key) >= 0;
}

function groupColor(event) {
  const slug = event.groups?.[0]?.slug || "community";
  let hash = 0;
  for (const character of slug) hash = (hash * 31 + character.charCodeAt(0)) | 0;
  return PALETTE[Math.abs(hash) % PALETTE.length];
}

function layoutBands(events, week) {
  const weekStart = dateKey(week[0]);
  const weekEnd = dateKey(week[6]);
  const segments = events
    .filter(isBandEvent)
    .map((event) => ({ event, bounds: eventBounds(event) }))
    .filter(({ bounds }) => bounds.start <= weekEnd && bounds.end >= weekStart)
    .map(({ event, bounds }) => ({
      event,
      bounds,
      start: Math.max(0, Math.round((dateFromKey(bounds.start) - week[0]) / DAY_MS)),
      end: Math.min(6, Math.round((dateFromKey(bounds.end) - week[0]) / DAY_MS)),
      continuesLeft: bounds.start < weekStart,
      continuesRight: bounds.end > weekEnd,
    }))
    .sort((left, right) =>
      left.start - right.start ||
      right.end - right.start - (left.end - left.start) ||
      left.event.title.localeCompare(right.event.title)
    );

  const laneEnds = [];
  for (const segment of segments) {
    let lane = laneEnds.findIndex((end) => end < segment.start);
    if (lane === -1) lane = laneEnds.length;
    laneEnds[lane] = segment.end;
    segment.lane = lane;
  }
  return segments;
}

function eventsForDay(key) {
  return state.events
    .filter((event) => overlapsDay(event, key))
    .sort((left, right) => {
      if (left.is_all_day !== right.is_all_day) return left.is_all_day ? -1 : 1;
      const leftStart = left.starts_at || `${left.start_date}T00:00:00Z`;
      const rightStart = right.starts_at || `${right.start_date}T00:00:00Z`;
      return leftStart.localeCompare(rightStart) || left.title.localeCompare(right.title);
    });
}

function formatTime(value) {
  return new Intl.DateTimeFormat(undefined, {
    timeZone: state.timezone,
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(value));
}

function formatDate(value, options) {
  return new Intl.DateTimeFormat(undefined, { timeZone: "UTC", ...options }).format(value);
}

function renderBand(segment) {
  const { event } = segment;
  const color = groupColor(event);
  const button = document.createElement("button");
  button.type = "button";
  button.className = [
    "band",
    segment.continuesLeft ? "continues-left" : "",
    segment.continuesRight ? "continues-right" : "",
  ].filter(Boolean).join(" ");
  button.style.gridColumn = `${segment.start + 1} / ${segment.end + 2}`;
  button.style.gridRow = String(segment.lane + 1);
  button.style.setProperty("--band-bg", color.soft);
  button.style.setProperty("--band-ink", color.ink);
  button.setAttribute("aria-label", eventAriaLabel(event));
  button.title = eventAriaLabel(event);

  if (!event.is_all_day && !segment.continuesLeft) {
    const time = document.createElement("span");
    time.className = "band-time";
    time.textContent = formatTime(event.starts_at);
    button.append(time);
  }
  const title = document.createElement("span");
  title.className = "band-title";
  title.textContent = event.title;
  button.append(title);
  button.addEventListener("click", () => openEventDialog(event));
  return button;
}

function renderTimedEvent(event) {
  const color = groupColor(event);
  const button = document.createElement("button");
  button.type = "button";
  button.className = "timed-event";
  button.setAttribute("aria-label", eventAriaLabel(event));
  button.style.setProperty("--event-color", color.solid);

  const dot = document.createElement("span");
  dot.className = "event-dot";
  dot.setAttribute("aria-hidden", "true");
  const time = document.createElement("span");
  time.className = "event-time";
  time.textContent = formatTime(event.starts_at);
  const title = document.createElement("span");
  title.className = "timed-title";
  title.textContent = event.title;
  button.append(dot, time, title);
  button.addEventListener("click", () => openEventDialog(event));
  return button;
}

function renderWeek(week) {
  const weekElement = document.createElement("div");
  weekElement.className = "calendar-week";
  weekElement.setAttribute("role", "row");
  const segments = layoutBands(state.events, week);
  const visibleBands = segments.filter((segment) => segment.lane < MAX_BAND_LANES);
  const visibleLaneCount = Math.min(
    MAX_BAND_LANES,
    visibleBands.reduce((highest, segment) => Math.max(highest, segment.lane + 1), 0)
  );
  weekElement.style.setProperty("--band-rows", visibleLaneCount);
  weekElement.style.setProperty("--band-space", `${visibleLaneCount * 25}px`);

  for (const day of week) {
    const key = dateKey(day);
    const dayEvents = eventsForDay(key);
    const timedEvents = dayEvents.filter((event) => !isBandEvent(event));
    const hiddenBands = segments.filter(
      (segment) => segment.lane >= MAX_BAND_LANES && overlapsDay(segment.event, key)
    ).length;
    const hiddenTimed = Math.max(0, timedEvents.length - MAX_TIMED_PER_DAY);
    const hiddenCount = hiddenBands + hiddenTimed;
    const cell = document.createElement("div");
    const isOutside = day.getUTCMonth() !== state.displayedMonth.getUTCMonth();
    const todayKey = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
    cell.className = [
      "day-cell",
      isOutside ? "outside-month" : "",
      key === todayKey ? "today" : "",
    ].filter(Boolean).join(" ");
    cell.setAttribute("role", "gridcell");
    cell.setAttribute("aria-label", formatDate(day, { weekday: "long", month: "long", day: "numeric", year: "numeric" }));

    const head = document.createElement("div");
    head.className = "day-head";
    const number = document.createElement("button");
    number.type = "button";
    number.className = "day-number";
    number.textContent = String(day.getUTCDate());
    number.setAttribute("aria-label", `View ${dayEvents.length} event${dayEvents.length === 1 ? "" : "s"} on ${cell.getAttribute("aria-label")}`);
    number.addEventListener("click", () => openDayDialog(key));
    head.append(number);

    const timedList = document.createElement("div");
    timedList.className = "timed-events";
    for (const event of timedEvents.slice(0, MAX_TIMED_PER_DAY)) {
      timedList.append(renderTimedEvent(event));
    }
    if (hiddenCount > 0) {
      const more = document.createElement("button");
      more.type = "button";
      more.className = "more-btn";
      more.textContent = `+${hiddenCount} more`;
      more.setAttribute("aria-label", `View ${hiddenCount} more event${hiddenCount === 1 ? "" : "s"} on ${cell.getAttribute("aria-label")}`);
      more.addEventListener("click", () => openDayDialog(key));
      timedList.append(more);
    }
    cell.append(head, timedList);
    weekElement.append(cell);
  }

  const bandLayer = document.createElement("div");
  bandLayer.className = "week-bands";
  bandLayer.setAttribute("aria-hidden", "false");
  for (const segment of visibleBands) bandLayer.append(renderBand(segment));
  weekElement.append(bandLayer);
  return weekElement;
}

function renderCalendar() {
  const weeks = visibleWeeks(state.displayedMonth);
  monthGrid.replaceChildren(...weeks.map(renderWeek));
  monthGrid.setAttribute(
    "aria-label",
    formatDate(state.displayedMonth, { month: "long", year: "numeric" })
  );
  monthLabel.textContent = formatDate(state.displayedMonth, { month: "long", year: "numeric" });
}

function accessToken() {
  const params = new URLSearchParams(window.location.search);
  return params.get("token") || params.get("access_token");
}

async function loadMonth() {
  if (state.request) state.request.abort();
  const controller = new AbortController();
  state.request = controller;
  const weeks = visibleWeeks(state.displayedMonth);
  const start = dateKey(weeks[0][0]);
  const end = dateKey(addDays(weeks.at(-1)[6], 1));
  const params = new URLSearchParams({
    start,
    end,
    timezone: state.timezone,
    limit: "2000",
  });
  const token = accessToken();
  if (token) params.set("token", token);

  calendarFrame.setAttribute("aria-busy", "true");
  statusRegion.textContent = "";
  try {
    const allEvents = [];
    let offset = 0;
    let hasMore = true;
    while (hasMore) {
      params.set("offset", String(offset));
      const response = await fetch(`/api/v1/calendar?${params}`, {
        signal: controller.signal,
        headers: { Accept: "application/json" },
      });
      if (!response.ok) {
        const body = await response.json().catch(() => null);
        throw new Error(body?.error?.message || `Calendar request failed (${response.status})`);
      }
      const body = await response.json();
      allEvents.push(...body.items);
      hasMore = body.meta.has_more;
      offset += body.items.length;
    }
    state.events = allEvents;
    renderCalendar();
    statusRegion.textContent = `${allEvents.length} approved event${allEvents.length === 1 ? "" : "s"}`;
  } catch (error) {
    if (error.name === "AbortError") return;
    state.events = [];
    renderCalendar();
    statusRegion.textContent = error.message;
  } finally {
    if (state.request === controller) {
      state.request = null;
      calendarFrame.setAttribute("aria-busy", "false");
    }
  }
}

function eventAriaLabel(event) {
  const timing = event.is_all_day ? "All day" : formatTime(event.starts_at);
  return `${timing}: ${event.title}`;
}

function dayEventMeta(event, key) {
  if (event.is_all_day) return "All day";
  const bounds = eventBounds(event);
  if (bounds.start < key) return `Continues · until ${formatTime(event.ends_at)}`;
  if (bounds.end > key) return `${formatTime(event.starts_at)} · continues`;
  return `${formatTime(event.starts_at)}–${formatTime(event.ends_at)}`;
}

function openDayDialog(key) {
  const day = dateFromKey(key);
  document.querySelector("#day-dialog-title").textContent = formatDate(day, {
    weekday: "long",
    month: "long",
    day: "numeric",
  });
  const container = document.querySelector("#day-dialog-events");
  const events = eventsForDay(key);
  const cards = events.map((event) => {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "day-event-card";
    card.style.setProperty("--event-color", groupColor(event).solid);
    const accent = document.createElement("span");
    accent.className = "card-accent";
    accent.setAttribute("aria-hidden", "true");
    const copy = document.createElement("span");
    copy.className = "day-card-copy";
    const title = document.createElement("span");
    title.className = "day-card-title";
    title.textContent = event.title;
    const meta = document.createElement("span");
    meta.className = "day-card-meta";
    const location = event.location_name ? ` · ${event.location_name}` : "";
    meta.textContent = `${dayEventMeta(event, key)}${location}`;
    copy.append(title, meta);
    const arrow = document.createElement("span");
    arrow.className = "card-arrow";
    arrow.setAttribute("aria-hidden", "true");
    arrow.textContent = "›";
    card.append(accent, copy, arrow);
    card.addEventListener("click", () => {
      dayDialog.close();
      openEventDialog(event);
    });
    return card;
  });
  if (cards.length === 0) {
    const empty = document.createElement("p");
    empty.className = "event-description";
    empty.textContent = "No approved events on this day.";
    cards.push(empty);
  }
  container.replaceChildren(...cards);
  dayDialog.showModal();
}

function formatEventSchedule(event) {
  if (event.is_all_day) {
    const start = dateFromKey(event.start_date);
    const end = addDays(dateFromKey(event.end_date), -1);
    const startText = formatDate(start, { weekday: "long", month: "long", day: "numeric", year: "numeric" });
    if (dateKey(start) === dateKey(end)) return `${startText} · All day`;
    const endText = formatDate(end, { weekday: "long", month: "long", day: "numeric", year: "numeric" });
    return `${startText} – ${endText} · All day`;
  }
  const start = new Date(event.starts_at);
  const end = new Date(event.ends_at);
  const startDay = localDateKey(start);
  const endDay = localDateKey(new Date(end.getTime() - 1));
  const dateFormatter = new Intl.DateTimeFormat(undefined, {
    timeZone: state.timezone,
    weekday: "long",
    month: "long",
    day: "numeric",
    year: "numeric",
  });
  if (startDay === endDay) {
    return `${dateFormatter.format(start)} · ${formatTime(event.starts_at)}–${formatTime(event.ends_at)}`;
  }
  return `${dateFormatter.format(start)}, ${formatTime(event.starts_at)} – ${dateFormatter.format(end)}, ${formatTime(event.ends_at)}`;
}

function metaRow(label, value) {
  const wrapper = document.createElement("div");
  wrapper.className = "event-meta-row";
  const term = document.createElement("dt");
  term.textContent = label;
  const detail = document.createElement("dd");
  detail.textContent = value;
  wrapper.append(term, detail);
  return wrapper;
}

function openEventDialog(event) {
  const groups = document.querySelector("#event-dialog-groups");
  groups.replaceChildren(...(event.groups || []).map((group) => {
    const pill = document.createElement("span");
    pill.className = "group-pill";
    pill.textContent = group.name;
    return pill;
  }));
  document.querySelector("#event-dialog-title").textContent = event.title;
  const meta = document.querySelector("#event-dialog-meta");
  const rows = [metaRow("When", formatEventSchedule(event))];
  const location = [event.location_name, event.location_address].filter(Boolean).join(" · ");
  if (location) rows.push(metaRow("Where", location));
  if (event.timezone && !event.is_all_day) rows.push(metaRow("Timezone", event.timezone.replaceAll("_", " ")));
  meta.replaceChildren(...rows);
  document.querySelector("#event-dialog-description").textContent = event.description || "";
  const link = document.querySelector("#event-dialog-link");
  let safeUrl = null;
  try {
    const candidate = new URL(event.event_url);
    if (["http:", "https:"].includes(candidate.protocol)) safeUrl = candidate.href;
  } catch (_) {
    safeUrl = null;
  }
  link.hidden = !safeUrl;
  if (safeUrl) link.href = safeUrl;
  eventDialog.showModal();
}

function moveMonth(offset) {
  state.displayedMonth = utcDate(
    state.displayedMonth.getUTCFullYear(),
    state.displayedMonth.getUTCMonth() + offset,
    1
  );
  loadMonth();
}

function renderMonthJump() {
  document.querySelector("#jump-year").textContent = String(state.jumpYear);
  const monthNames = Array.from({ length: 12 }, (_, month) =>
    formatDate(utcDate(2024, month, 1), { month: "short" })
  );
  const buttons = monthNames.map((name, month) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = name;
    const selected = state.jumpYear === state.displayedMonth.getUTCFullYear() && month === state.displayedMonth.getUTCMonth();
    const current = state.jumpYear === now.getFullYear() && month === now.getMonth();
    button.className = [selected ? "selected" : "", current ? "current" : ""].filter(Boolean).join(" ");
    button.setAttribute("aria-current", selected ? "date" : "false");
    button.addEventListener("click", () => {
      state.displayedMonth = utcDate(state.jumpYear, month, 1);
      monthJump.close();
      loadMonth();
    });
    return button;
  });
  document.querySelector("#jump-months").replaceChildren(...buttons);
}

function jumpYear(offset) {
  state.jumpYear += offset;
  renderMonthJump();
}

function bindControls() {
  const token = accessToken();
  if (token) document.querySelector(".brand").href = `/?token=${encodeURIComponent(token)}`;
  document.querySelector("#previous-month").addEventListener("click", () => moveMonth(-1));
  document.querySelector("#next-month").addEventListener("click", () => moveMonth(1));
  document.querySelector("#today-button").addEventListener("click", () => {
    state.displayedMonth = utcDate(now.getFullYear(), now.getMonth(), 1);
    loadMonth();
  });
  document.querySelector("#month-heading").addEventListener("click", () => {
    state.jumpYear = state.displayedMonth.getUTCFullYear();
    renderMonthJump();
    monthJump.showModal();
  });
  document.querySelector("#jump-prev-year").addEventListener("click", () => jumpYear(-1));
  document.querySelector("#jump-next-year").addEventListener("click", () => jumpYear(1));
  document.querySelectorAll("[data-close]").forEach((button) => {
    button.addEventListener("click", () => document.querySelector(`#${button.dataset.close}`).close());
  });
  for (const dialog of document.querySelectorAll("dialog")) {
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) dialog.close();
    });
  }
  document.addEventListener("keydown", (event) => {
    if (document.querySelector("dialog[open]")) return;
    if (event.key === "ArrowLeft" && event.altKey) moveMonth(-1);
    if (event.key === "ArrowRight" && event.altKey) moveMonth(1);
  });
}

bindControls();
renderCalendar();
loadMonth();
