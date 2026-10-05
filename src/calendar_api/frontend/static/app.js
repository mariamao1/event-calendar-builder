"use strict";

const MAX_BAND_LANES = 3;
const MAX_TIMED_PER_DAY = 3;
const MAX_ALLDAY_ROWS = 3;
const DAY_MS = 86_400_000;
const MINUTES_PER_DAY = 1440;
// Visible hour-range policy for the week/day time grids: the grid always
// covers TIME_VIEW_DEFAULT_START..TIME_VIEW_DEFAULT_END (business hours) and
// adaptively expands — with one hour of padding — to include the earliest
// start and latest end of the timed events actually present, clamped to the
// full 0..24 day. All-day events never affect the range; they live in their
// own all-day region above the time grid.
const TIME_VIEW_DEFAULT_START = 7;
const TIME_VIEW_DEFAULT_END = 19;
const TIME_VIEW_RANGE_PADDING_HOURS = 1;
const MIN_DISPLAY_MINUTES = 30;
const HOUR_PX = 52;
const PALETTE = [
  { solid: "#447d68", soft: "#dceae4", ink: "#173d31" },
  { solid: "#d8674b", soft: "#fae4dc", ink: "#74301f" },
  { solid: "#4f8097", soft: "#dce9ef", ink: "#244b5d" },
  { solid: "#b98420", soft: "#f4e7c7", ink: "#624711" },
  { solid: "#7c6ca6", soft: "#e9e3f2", ink: "#493b70" },
];

const monthGrid = document.querySelector("#month-grid");
const weekdayRow = document.querySelector("#month-weekday-row");
const weekView = document.querySelector("#week-view");
const dayView = document.querySelector("#day-view");
const weekAllDay = document.querySelector("#week-allday");
const dayAllDay = document.querySelector("#day-allday");
const weekGrid = document.querySelector("#week-grid");
const dayGrid = document.querySelector("#day-grid");
const viewSwitcher = document.querySelector("#view-switcher");
const calendarFrame = document.querySelector(".calendar-frame");
const monthLabel = document.querySelector("#month-label");
const statusRegion = document.querySelector("#status-region");
const monthJump = document.querySelector("#month-jump");
const dayDialog = document.querySelector("#day-dialog");
const eventDialog = document.querySelector("#event-dialog");

const now = new Date();

// View persistence through the link URL: the selected view and cursor date
// live in ?view=…&date=… so a refresh or a shared link lands in the same
// place. Values are validated on load; anything missing or malformed falls
// back to the month view anchored at today. Other query params (notably the
// link token) are left untouched.
function parseAnchor(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  const parsed = dateFromKey(value);
  if (Number.isNaN(parsed.getTime())) return null;
  // Reject rollover dates such as month 13 (Date.UTC normalizes them).
  return dateKey(parsed) === value ? parsed : null;
}

function readUrlState() {
  const params = new URLSearchParams(window.location.search);
  const view = params.get("view");
  return {
    view: ["month", "week", "day"].includes(view) ? view : "month",
    anchor: parseAnchor(params.get("date")),
  };
}

function writeUrlState() {
  try {
    const url = new URL(window.location.href);
    url.searchParams.set("view", state.view);
    const anchor = state.view === "month" ? state.displayedMonth : state.anchorDate;
    url.searchParams.set("date", dateKey(anchor));
    // Replace, don't push: stepping through weeks shouldn't flood history.
    window.history.replaceState(null, "", url);
  } catch (_) {
    // URL persistence is a convenience, never a requirement.
  }
}

const urlState = readUrlState();
const initialAnchor = urlState.anchor ||
  new Date(Date.UTC(now.getFullYear(), now.getMonth(), now.getDate()));
const state = {
  view: urlState.view,
  displayedMonth: utcDate(initialAnchor.getUTCFullYear(), initialAnchor.getUTCMonth(), 1),
  anchorDate: initialAnchor,
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

function weekDays(anchor) {
  const key = dateKey(anchor);
  const start = addDays(dateFromKey(key), -dateFromKey(key).getUTCDay());
  return Array.from({ length: 7 }, (_, index) => addDays(start, index));
}

function localTimeParts(instant) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: state.timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(instant);
  const pick = (type) => parts.find((part) => part.type === type).value;
  return {
    key: `${pick("year")}-${pick("month")}-${pick("day")}`,
    minutes: Number(pick("hour")) * 60 + Number(pick("minute")),
  };
}

// A per-day slice of a timed event, clipped to [0, 1440) wall minutes of the
// given local day key. All-day events never produce segments — they belong in
// the all-day region. Multi-day timed events produce one clipped segment per
// overlapped day with continuesBefore/After flags. Midnight endpoints stay
// exclusive via eventBounds (an event ending at 00:00 belongs to the prior day).
function timedSegmentsForDay(key) {
  const segments = [];
  for (const event of state.events) {
    if (event.is_all_day || !event.starts_at || !event.ends_at) continue;
    const bounds = eventBounds(event);
    if (compareKeys(bounds.start, key) > 0 || compareKeys(bounds.end, key) < 0) continue;
    const startParts = localTimeParts(new Date(event.starts_at));
    const endParts = localTimeParts(new Date(event.ends_at));
    const startInstant = new Date(event.starts_at).getTime();
    const endInstant = new Date(event.ends_at).getTime();
    let startMin;
    let endMin;
    if (bounds.start === key && bounds.end === key) {
      startMin = startParts.minutes;
      // Zero-length or backwards endpoints still occupy a visible block.
      endMin = Math.max(endParts.minutes, startParts.minutes + MIN_DISPLAY_MINUTES);
      if (endInstant <= startInstant) endMin = startParts.minutes + MIN_DISPLAY_MINUTES;
      // A midnight-exclusive endpoint collapses bounds to this single day
      // while the end instant already falls on the next local day.
      if (endParts.key !== key) endMin = MINUTES_PER_DAY;
    } else if (bounds.start === key) {
      startMin = startParts.minutes;
      endMin = MINUTES_PER_DAY;
    } else if (bounds.end === key) {
      startMin = 0;
      endMin = Math.max(endParts.minutes, MIN_DISPLAY_MINUTES);
    } else {
      startMin = 0;
      endMin = MINUTES_PER_DAY;
    }
    startMin = Math.max(0, Math.min(MINUTES_PER_DAY, startMin));
    endMin = Math.max(startMin + MIN_DISPLAY_MINUTES, Math.min(MINUTES_PER_DAY, endMin));
    // A midnight-exclusive endpoint yields an empty slice on its end day;
    // eventBounds already excluded that day, so reaching here with an empty
    // slice only happens for zero-length data — keep the minimum block.
    segments.push({
      event,
      startMin,
      endMin,
      continuesBefore: bounds.start < key,
      continuesAfter: bounds.end > key,
    });
  }
  segments.sort((a, b) =>
    a.startMin - b.startMin ||
    b.endMin - b.startMin - (a.endMin - a.startMin) ||
    a.event.title.localeCompare(b.event.title)
  );
  return segments;
}

// Classic calendar overlap layout: sweep segments into clusters where every
// member overlaps (transitively), then greedily color each cluster into the
// fewest side-by-side columns. Each segment gets {column, columnCount} so it
// renders at left = column/columnCount, width = 1/columnCount. Non-overlapping
// segments each get the full width.
function layoutTimedColumns(segments) {
  const clusters = [];
  let active = [];
  let clusterMaxEnd = -1;
  const ordered = [...segments].sort((a, b) => a.startMin - b.startMin || a.endMin - b.endMin);
  for (const segment of ordered) {
    if (active.length === 0 || segment.startMin < clusterMaxEnd) {
      active.push(segment);
      clusterMaxEnd = Math.max(clusterMaxEnd, segment.endMin);
    } else {
      clusters.push(active);
      active = [segment];
      clusterMaxEnd = segment.endMin;
    }
  }
  if (active.length > 0) clusters.push(active);

  for (const cluster of clusters) {
    const laneEnds = [];
    for (const segment of cluster) {
      let lane = laneEnds.findIndex((end) => end <= segment.startMin);
      if (lane === -1) lane = laneEnds.length;
      laneEnds[lane] = segment.endMin;
      segment.column = lane;
    }
    const columnCount = Math.max(1, laneEnds.length);
    for (const segment of cluster) segment.columnCount = columnCount;
  }
  for (const segment of ordered) {
    if (segment.column === undefined) segment.column = 0;
    if (segment.columnCount === undefined) segment.columnCount = 1;
  }
  return ordered;
}

function allDayEventsForDay(key) {
  return state.events
    .filter((event) => event.is_all_day && overlapsDay(event, key))
    .sort((a, b) =>
      (a.start_date || "").localeCompare(b.start_date || "") ||
      a.title.localeCompare(b.title)
    );
}

// Adaptive hour range over the visible day keys: business-hours default,
// expanded with padding to enclose every timed segment present.
function computeVisibleHours(dayKeys) {
  let earliest = null;
  let latest = null;
  for (const key of dayKeys) {
    for (const segment of timedSegmentsForDay(key)) {
      earliest = earliest === null ? segment.startMin : Math.min(earliest, segment.startMin);
      latest = latest === null ? segment.endMin : Math.max(latest, segment.endMin);
    }
  }
  let startHour = TIME_VIEW_DEFAULT_START;
  let endHour = TIME_VIEW_DEFAULT_END;
  if (earliest !== null && latest !== null) {
    startHour = Math.min(
      TIME_VIEW_DEFAULT_START,
      Math.floor((earliest - TIME_VIEW_RANGE_PADDING_HOURS * 60) / 60)
    );
    endHour = Math.max(
      TIME_VIEW_DEFAULT_END,
      Math.ceil((latest + TIME_VIEW_RANGE_PADDING_HOURS * 60) / 60)
    );
  }
  startHour = Math.max(0, startHour);
  endHour = Math.min(24, Math.max(startHour + 1, endHour));
  return { startHour, endHour };
}

function formatHourLabel(hour) {
  const anchor = new Date(Date.UTC(2024, 0, 1, hour, 0));
  return new Intl.DateTimeFormat(undefined, {
    timeZone: "UTC",
    hour: "numeric",
  }).format(anchor);
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
    cancelledClass(event),
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
  button.className = ["timed-event", cancelledClass(event)].filter(Boolean).join(" ");
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
  updateHeading();
  for (const button of viewSwitcher.querySelectorAll("[data-view]")) {
    const selected = button.dataset.view === state.view;
    button.setAttribute("aria-pressed", String(selected));
    button.classList.toggle("active", selected);
  }
  const showMonth = state.view === "month";
  const showWeek = state.view === "week";
  monthGrid.hidden = !showMonth;
  weekdayRow.hidden = !showMonth;
  weekView.hidden = !showWeek;
  dayView.hidden = showMonth || showWeek;
  if (showMonth) {
    const weeks = visibleWeeks(state.displayedMonth);
    monthGrid.replaceChildren(...weeks.map(renderWeek));
    monthGrid.setAttribute(
      "aria-label",
      formatDate(state.displayedMonth, { month: "long", year: "numeric" })
    );
  } else if (showWeek) {
    renderTimeView(weekDays(state.anchorDate), weekAllDay, weekGrid);
  } else {
    renderTimeView([state.anchorDate], dayAllDay, dayGrid);
  }
}

function updateHeading() {
  if (state.view === "week") {
    const days = weekDays(state.anchorDate);
    const first = formatDate(days[0], { month: "short", day: "numeric" });
    const last = formatDate(days[6], { month: "short", day: "numeric", year: "numeric" });
    monthLabel.textContent = `${first} – ${last}`;
  } else if (state.view === "day") {
    monthLabel.textContent = formatDate(state.anchorDate, {
      weekday: "short",
      month: "long",
      day: "numeric",
      year: "numeric",
    });
  } else {
    monthLabel.textContent = formatDate(state.displayedMonth, { month: "long", year: "numeric" });
  }
}

function renderTimeBlock(segment, rangeStartMin, rangeMinutes) {
  const { event } = segment;
  const color = groupColor(event);
  const button = document.createElement("button");
  button.type = "button";
  button.className = [
    "time-block",
    segment.continuesBefore ? "continues-before" : "",
    segment.continuesAfter ? "continues-after" : "",
    cancelledClass(event),
  ].filter(Boolean).join(" ");
  const clampedStart = Math.max(segment.startMin, rangeStartMin);
  const clampedEnd = Math.min(segment.endMin, rangeStartMin + rangeMinutes);
  const top = ((clampedStart - rangeStartMin) / rangeMinutes) * 100;
  const height = Math.max(((clampedEnd - clampedStart) / rangeMinutes) * 100, 3.2);
  const width = 100 / segment.columnCount;
  button.style.top = `${top}%`;
  button.style.height = `${height}%`;
  button.style.left = `calc(${(segment.column * width).toFixed(4)}% + 2px)`;
  button.style.width = `calc(${width.toFixed(4)}% - 4px)`;
  button.style.setProperty("--event-color", color.solid);
  button.setAttribute("aria-label", eventAriaLabel(event));
  button.title = eventAriaLabel(event);

  const time = document.createElement("span");
  time.className = "time-block-time";
  time.textContent = event.is_all_day
    ? "All day"
    : `${formatTime(event.starts_at)}–${formatTime(event.ends_at)}`;
  const title = document.createElement("span");
  title.className = "time-block-title";
  title.textContent =
    (segment.continuesBefore ? "↩ " : "") + event.title + (segment.continuesAfter ? " ↪" : "");
  button.append(time, title);
  button.addEventListener("click", () => openEventDialog(event));
  return button;
}

function renderTimeView(days, allDayContainer, gridContainer) {
  const dayKeys = days.map(dateKey);
  const { startHour, endHour } = computeVisibleHours(dayKeys);
  const rangeStartMin = startHour * 60;
  const rangeMinutes = (endHour - startHour) * 60;
  const gridHeight = (endHour - startHour) * HOUR_PX;

  allDayContainer.replaceChildren();
  const corner = document.createElement("span");
  corner.className = "allday-title";
  corner.textContent = "All-day";
  allDayContainer.append(corner);
  const allDayGrid = document.createElement("div");
  allDayGrid.className = "allday-grid";
  allDayGrid.style.gridTemplateColumns = `repeat(${days.length}, minmax(0, 1fr))`;
  for (const day of days) {
    const key = dateKey(day);
    const cell = document.createElement("div");
    cell.className = "allday-cell";
    const events = allDayEventsForDay(key);
    for (const event of events.slice(0, MAX_ALLDAY_ROWS)) {
      const color = groupColor(event);
      const button = document.createElement("button");
      button.type = "button";
      button.className = ["allday-chip", cancelledClass(event)].filter(Boolean).join(" ");
      button.style.setProperty("--band-bg", color.soft);
      button.style.setProperty("--band-ink", color.ink);
      button.textContent = event.title;
      button.title = eventAriaLabel(event);
      button.setAttribute("aria-label", `${eventAriaLabel(event)} on ${formatDate(day, { month: "long", day: "numeric" })}`);
      button.addEventListener("click", () => openEventDialog(event));
      cell.append(button);
    }
    if (events.length > MAX_ALLDAY_ROWS) {
      const more = document.createElement("button");
      more.type = "button";
      more.className = "more-btn";
      more.textContent = `+${events.length - MAX_ALLDAY_ROWS} more`;
      more.setAttribute("aria-label", `View all-day events on ${formatDate(day, { month: "long", day: "numeric" })}`);
      more.addEventListener("click", () => openDayDialog(key));
      cell.append(more);
    }
    if (events.length === 0) {
      const empty = document.createElement("span");
      empty.className = "allday-empty";
      empty.setAttribute("aria-hidden", "true");
      empty.textContent = "—";
      cell.append(empty);
    }
    allDayGrid.append(cell);
  }
  allDayContainer.append(allDayGrid);

  gridContainer.replaceChildren();
  gridContainer.style.setProperty("--grid-height", `${gridHeight}px`);

  const gutter = document.createElement("div");
  gutter.className = "time-gutter";
  gutter.setAttribute("aria-hidden", "true");
  const gutterHead = document.createElement("div");
  gutterHead.className = "time-gutter-head";
  gutter.append(gutterHead);
  const gutterBody = document.createElement("div");
  gutterBody.className = "time-gutter-body";
  gutterBody.style.height = `${gridHeight}px`;
  for (let hour = startHour; hour < endHour; hour++) {
    const label = document.createElement("span");
    label.className = "hour-label";
    label.style.height = `${HOUR_PX}px`;
    label.textContent = formatHourLabel(hour);
    gutterBody.append(label);
  }
  gutter.append(gutterBody);
  gridContainer.append(gutter);

  const headerRow = document.createElement("div");
  headerRow.className = "time-header-row";
  headerRow.style.gridTemplateColumns = `repeat(${days.length}, minmax(0, 1fr))`;
  const columnsRow = document.createElement("div");
  columnsRow.className = "time-columns";
  columnsRow.style.gridTemplateColumns = `repeat(${days.length}, minmax(0, 1fr))`;

  const nowParts = localTimeParts(new Date());
  const todayKey = localDateKey(new Date());
  days.forEach((day, index) => {
    const key = dateKey(day);
    const header = document.createElement("div");
    const isToday = key === todayKey;
    header.className = ["time-header", isToday ? "today" : ""].filter(Boolean).join(" ");
    header.setAttribute("role", "columnheader");
    const weekday = document.createElement("span");
    weekday.className = "time-header-weekday";
    weekday.textContent = formatDate(day, { weekday: "short" });
    const number = document.createElement("button");
    number.type = "button";
    number.className = "day-number";
    number.textContent = String(day.getUTCDate());
    number.setAttribute("aria-label", `View ${formatDate(day, { weekday: "long", month: "long", day: "numeric" })}`);
    number.addEventListener("click", () => {
      state.anchorDate = day;
      setView("day");
    });
    header.append(weekday, number);
    headerRow.append(header);

    const column = document.createElement("div");
    column.className = ["time-column", isToday ? "today" : ""].filter(Boolean).join(" ");
    column.setAttribute("role", "gridcell");
    column.setAttribute("aria-label", formatDate(day, { weekday: "long", month: "long", day: "numeric" }));
    column.style.height = `${gridHeight}px`;
    for (let hour = startHour; hour < endHour; hour++) {
      const line = document.createElement("div");
      line.className = "hour-line";
      line.style.height = `${HOUR_PX}px`;
      column.append(line);
    }
    const lane = document.createElement("div");
    lane.className = "time-lane";
    const segments = layoutTimedColumns(timedSegmentsForDay(key));
    for (const segment of segments) {
      if (segment.endMin <= rangeStartMin || segment.startMin >= rangeStartMin + rangeMinutes) continue;
      lane.append(renderTimeBlock(segment, rangeStartMin, rangeMinutes));
    }
    if (isToday && nowParts.key === key) {
      const minutes = nowParts.minutes;
      if (minutes >= rangeStartMin && minutes <= rangeStartMin + rangeMinutes) {
        const marker = document.createElement("div");
        marker.className = "now-line";
        marker.style.top = `${((minutes - rangeStartMin) / rangeMinutes) * 100}%`;
        marker.setAttribute("aria-hidden", "true");
        lane.append(marker);
      }
    }
    column.append(lane);
    columnsRow.append(column);
    void index;
  });

  const body = document.createElement("div");
  body.className = "time-body";
  body.append(headerRow, columnsRow);
  gridContainer.append(body);
  gridContainer.dataset.startHour = String(startHour);
  gridContainer.dataset.endHour = String(endHour);
}

function visibleDays() {
  if (state.view === "week") return weekDays(state.anchorDate);
  if (state.view === "day") return [state.anchorDate];
  return null;
}

function visibleFetchRange() {
  if (state.view === "week") {
    const days = weekDays(state.anchorDate);
    return { start: dateKey(days[0]), end: dateKey(addDays(days[6], 1)) };
  }
  if (state.view === "day") {
    return { start: dateKey(state.anchorDate), end: dateKey(addDays(state.anchorDate, 1)) };
  }
  const weeks = visibleWeeks(state.displayedMonth);
  return { start: dateKey(weeks[0][0]), end: dateKey(addDays(weeks.at(-1)[6], 1)) };
}

function setView(view) {
  if (!["month", "week", "day"].includes(view)) return;
  state.view = view;
  if (view !== "month") {
    state.displayedMonth = utcDate(
      state.anchorDate.getUTCFullYear(),
      state.anchorDate.getUTCMonth(),
      1
    );
  }
  loadMonth();
}

function moveStep(offset) {
  if (state.view === "week") {
    state.anchorDate = addDays(state.anchorDate, offset * 7);
  } else if (state.view === "day") {
    state.anchorDate = addDays(state.anchorDate, offset);
  } else {
    moveMonth(offset);
    return;
  }
  loadMonth();
}

function accessToken() {
  const params = new URLSearchParams(window.location.search);
  return params.get("token") || params.get("access_token");
}

async function loadMonth() {
  // The view and cursor date are reflected in the URL on every navigation,
  // so a refresh or a shared link restores exactly what was visible.
  writeUrlState();
  if (state.request) state.request.abort();
  const controller = new AbortController();
  state.request = controller;
  const { start, end } = visibleFetchRange();
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
  return `${timing}: ${event.title}${event.is_cancelled ? " (cancelled)" : ""}`;
}

function cancelledClass(event) {
  return event.is_cancelled ? "is-cancelled" : "";
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
    card.className = ["day-event-card", cancelledClass(event)].filter(Boolean).join(" ");
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
    meta.textContent = `${event.is_cancelled ? "Cancelled · " : ""}${dayEventMeta(event, key)}${location}`;
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
  if (value instanceof Node) detail.append(value);
  else detail.textContent = value;
  wrapper.append(term, detail);
  return wrapper;
}

function groupPills(groups) {
  const list = document.createElement("span");
  list.className = "event-group-list";
  for (const group of groups) {
    const pill = document.createElement("span");
    pill.className = "group-pill";
    pill.textContent = group.name;
    list.append(pill);
  }
  return list;
}

function safeEventUrl(value) {
  try {
    const candidate = new URL(value);
    if (["http:", "https:"].includes(candidate.protocol)) return candidate.href;
  } catch (_) {
    // Missing or malformed URLs are simply not linked.
  }
  return null;
}

const WEEKDAY_OFFSETS = { SU: 0, MO: 1, TU: 2, WE: 3, TH: 4, FR: 5, SA: 6 };
const RECURRENCE_UNITS = {
  DAILY: ["day", "days"],
  WEEKLY: ["week", "weeks"],
  MONTHLY: ["month", "months"],
  YEARLY: ["year", "years"],
};
const DESCRIBED_RRULE_PARTS = new Set(["FREQ", "INTERVAL", "BYDAY", "BYMONTHDAY", "COUNT", "UNTIL", "WKST"]);

function listText(items) {
  try {
    return new Intl.ListFormat(undefined, { style: "long", type: "conjunction" }).format(items);
  } catch (_) {
    return items.join(", ");
  }
}

function ordinalText(value) {
  if (value === -1) return "last";
  if (value < -1) return `${ordinalText(-value)} to last`;
  const suffix = { 1: "st", 2: "nd", 3: "rd" }[value % 100 > 10 && value % 100 < 14 ? 0 : value % 10] || "th";
  return `${value}${suffix}`;
}

// Plain-language summary of the RRULE subset the event form produces, plus
// the common BYDAY/BYMONTHDAY/COUNT/UNTIL variants. Anything richer is shown
// as a custom schedule rather than risking a misleading sentence. Without
// BYDAY/BYMONTHDAY a rule repeats on the series start's weekday/day.
function describeRecurrence(rule, startKey = null) {
  if (!rule) return "";
  const parts = {};
  for (const piece of rule.split(";")) {
    const [key, value] = piece.split("=");
    if (key && value) parts[key.toUpperCase()] = value.toUpperCase();
  }
  const units = RECURRENCE_UNITS[parts.FREQ];
  const interval = Number(parts.INTERVAL || 1);
  const known = Object.keys(parts).every((key) => DESCRIBED_RRULE_PARTS.has(key));
  if (!units || !known || !Number.isInteger(interval) || interval < 1) {
    return "Repeats on a custom schedule";
  }
  let text = interval === 1 ? `Every ${units[0]}` : `Every ${interval} ${units[1]}`;
  const days = parts.BYDAY ? parts.BYDAY.split(",") : [];
  const weekdays = ["MO", "TU", "WE", "TH", "FR"];
  if (parts.FREQ === "WEEKLY" && interval === 1 && days.length === 5 && weekdays.every((day) => days.includes(day))) {
    text = "Every weekday";
  } else if (days.length > 0) {
    const names = days.map((day) => {
      const match = day.match(/^([+-]?\d{1,2})?(SU|MO|TU|WE|TH|FR|SA)$/);
      if (!match) return null;
      const name = formatDate(utcDate(2024, 0, 7 + WEEKDAY_OFFSETS[match[2]]), { weekday: "long" });
      return match[1] ? `the ${ordinalText(Number(match[1]))} ${name}` : name;
    });
    if (names.includes(null)) return "Repeats on a custom schedule";
    text += ` on ${listText(names)}`;
  } else if (parts.BYMONTHDAY) {
    text += ` on day ${listText(parts.BYMONTHDAY.split(","))}`;
  } else if (startKey) {
    const start = dateFromKey(startKey);
    if (parts.FREQ === "WEEKLY") text += ` on ${formatDate(start, { weekday: "long" })}`;
    if (parts.FREQ === "MONTHLY") text += ` on day ${start.getUTCDate()}`;
    if (parts.FREQ === "YEARLY") text += ` on ${formatDate(start, { month: "long", day: "numeric" })}`;
  }
  if (parts.COUNT) {
    text += ` · ${parts.COUNT} time${parts.COUNT === "1" ? "" : "s"}`;
  } else if (/^\d{8}/.test(parts.UNTIL || "")) {
    const until = utcDate(Number(parts.UNTIL.slice(0, 4)), Number(parts.UNTIL.slice(4, 6)) - 1, Number(parts.UNTIL.slice(6, 8)));
    text += ` · until ${formatDate(until, { month: "short", day: "numeric", year: "numeric" })}`;
  } else {
    text += " · no end date";
  }
  return text;
}

function occurrenceDayKey(occurrence) {
  return occurrence.is_all_day ? occurrence.start_date : localDateKey(new Date(occurrence.starts_at));
}

function formatOccurrenceLabel(occurrence) {
  const day = formatDate(dateFromKey(occurrenceDayKey(occurrence)), {
    weekday: "short",
    month: "short",
    day: "numeric",
    year: "numeric",
  });
  return occurrence.is_all_day ? `${day} · All day` : `${day} · ${formatTime(occurrence.starts_at)}`;
}

function formatLocalDates(items) {
  return items.map((item) => formatDate(dateFromKey(String(item.local_start).slice(0, 10)), {
    weekday: "short",
    month: "short",
    day: "numeric",
    year: "numeric",
  })).join("\n");
}

// The event detail view. Clicking an event renders it at once from the
// calendar row, then refines it with the published detail (series context)
// and, for signed-in admins, moderation details. While it is open the event
// lives in the URL as ?event=…&occurrence=…, so the address bar is itself a
// link back to this event; "Copy link" shares the same event with the
// calendar positioned on its date.
const eventDetail = { eventId: null, occurrenceId: null, request: null, view: null };
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function writeEventUrl() {
  try {
    const url = new URL(window.location.href);
    if (eventDetail.eventId) url.searchParams.set("event", eventDetail.eventId);
    else url.searchParams.delete("event");
    if (eventDetail.eventId && eventDetail.occurrenceId) {
      url.searchParams.set("occurrence", eventDetail.occurrenceId);
    } else {
      url.searchParams.delete("occurrence");
    }
    window.history.replaceState(null, "", url);
  } catch (_) {
    // URL persistence is a convenience, never a requirement.
  }
}

function eventShareUrl(eventId, occurrenceId, dayKey) {
  // Starts from the current address so a private calendar's link token is
  // kept; session-only credentials (admin or creator tokens) never are.
  const url = new URL(window.location.href);
  url.hash = "";
  url.searchParams.set("view", state.view);
  if (dayKey) url.searchParams.set("date", dayKey);
  url.searchParams.set("event", eventId);
  if (occurrenceId) url.searchParams.set("occurrence", occurrenceId);
  else url.searchParams.delete("occurrence");
  return url.href;
}

function setEventDetailError(message) {
  document.querySelector("#event-dialog-error").textContent = message || "";
}

function openEventDialog(event) {
  eventDetail.eventId = event.event_id;
  eventDetail.occurrenceId = event.occurrence_id || null;
  renderEventDetail({ event, timing: event, timingLabel: "When", loading: true });
  writeEventUrl();
  if (!eventDialog.open) eventDialog.showModal();
  loadEventDetail();
}

function openEventFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const eventId = params.get("event");
  if (!eventId || !UUID_PATTERN.test(eventId)) return;
  const occurrenceId = params.get("occurrence");
  eventDetail.eventId = eventId;
  eventDetail.occurrenceId = occurrenceId && UUID_PATTERN.test(occurrenceId) ? occurrenceId : null;
  writeEventUrl();
  renderEventDetail({ event: null, loading: true });
  eventDialog.showModal();
  loadEventDetail();
}

function selectSeriesOccurrence(occurrence) {
  eventDetail.occurrenceId = occurrence.occurrence_id;
  writeEventUrl();
  loadEventDetail();
}

async function loadEventDetail() {
  if (eventDetail.request) eventDetail.request.abort();
  const controller = new AbortController();
  eventDetail.request = controller;
  const { eventId, occurrenceId } = eventDetail;
  const path = new URL(`/api/v1/events/${eventId}`, window.location.origin);
  if (occurrenceId) path.searchParams.set("occurrence", occurrenceId);
  const surface = eventDialog.querySelector(".event-dialog-surface");
  surface.setAttribute("aria-busy", "true");
  setEventDetailError("");

  const publicRequest = jsonRequest(urlWithAccessToken(`${path.pathname}${path.search}`), {
    signal: controller.signal,
    headers: { Accept: "application/json" },
  });
  const adminRequest = isAdminSignedIn()
    ? jsonRequest(`/api/v1/admin/events/${eventId}`, {
      signal: controller.signal,
      headers: reviewQueueHeaders(),
    })
    : Promise.resolve(null);
  const [publicResult, adminResult] = await Promise.allSettled([publicRequest, adminRequest]);
  if (eventDetail.request !== controller) return;
  eventDetail.request = null;
  surface.setAttribute("aria-busy", "false");

  let admin = adminResult.status === "fulfilled" ? adminResult.value : null;
  let adminError = null;
  if (adminResult.status === "rejected") {
    adminError = handleReviewAuthError(adminResult.reason) || adminResult.reason.message;
    admin = null;
  }
  if (publicResult.status === "rejected") {
    const error = publicResult.reason;
    renderEventDetail({
      event: null,
      unavailable: error.status === 404
        ? "This event isn’t on the calendar. It may have been unpublished, or it hasn’t been approved yet."
        : error.message,
    });
    return;
  }

  const detail = publicResult.value;
  const series = detail.series;
  let timing = detail;
  let timingLabel = series ? "First date" : "When";
  if (detail.occurrence) {
    timing = detail.occurrence;
    timingLabel = "When";
  } else if (series?.upcoming?.length) {
    timing = series.upcoming[0];
    timingLabel = "Next date";
  }
  renderEventDetail({
    event: detail,
    timing,
    timingLabel,
    series,
    admin,
    adminError,
    missingOccurrence: Boolean(occurrenceId) && !detail.occurrence,
  });
}

function renderEventDetail(view) {
  eventDetail.view = view;
  const { event } = view;
  const eyebrow = document.querySelector("#event-dialog-eyebrow");
  const title = document.querySelector("#event-dialog-title");
  const status = document.querySelector("#event-dialog-status");
  const meta = document.querySelector("#event-dialog-meta");
  const description = document.querySelector("#event-dialog-description");
  const link = document.querySelector("#event-dialog-link");
  const footnote = document.querySelector("#event-dialog-footnote");
  const recurring = Boolean(view.series || event?.recurrence_rule);

  eyebrow.textContent = recurring ? "Repeating event" : "Event";
  status.textContent = "";
  status.classList.remove("is-cancelled");
  if (!event) {
    title.textContent = view.unavailable ? "Event unavailable" : "Loading event…";
    status.textContent = view.unavailable || "";
    meta.replaceChildren();
    meta.hidden = true;
    description.textContent = "";
    link.hidden = true;
    footnote.textContent = "";
    renderSeriesSection(null);
    renderAdminPanel(view);
    renderEventActions(view);
    return;
  }

  title.textContent = event.title;
  if (event.is_cancelled) {
    status.textContent = "This event is cancelled.";
    status.classList.add("is-cancelled");
  }
  if (view.missingOccurrence) {
    status.textContent = [
      status.textContent,
      "That date is no longer on the calendar. Showing the rest of this event.",
    ].filter(Boolean).join(" ");
  }
  const rows = [metaRow(view.timingLabel || "When", formatEventSchedule(view.timing || event))];
  const location = [event.location_name, event.location_address].filter(Boolean).join(" · ");
  if (location) rows.push(metaRow("Where", location));
  if (event.timezone && !(view.timing || event).is_all_day) {
    const eventZone = event.timezone.replaceAll("_", " ");
    rows.push(metaRow(
      "Timezone",
      event.timezone === state.timezone
        ? eventZone
        : `${eventZone} · times shown in your timezone (${state.timezone.replaceAll("_", " ")})`
    ));
  }
  if (event.groups?.length) rows.push(metaRow(event.groups.length === 1 ? "Group" : "Groups", groupPills(event.groups)));
  meta.replaceChildren(...rows);
  meta.hidden = false;
  description.textContent = event.description || "";
  const safeUrl = safeEventUrl(event.event_url);
  link.hidden = !safeUrl;
  if (safeUrl) link.href = safeUrl;

  const published = event.reviewed_at
    ? formatSubmittedAt(event.reviewed_at)
    : null;
  footnote.textContent = published
    ? `${event.revision_number > 1 ? "Last updated" : "Published"} ${published}`
    : "";

  renderSeriesSection(view);
  renderAdminPanel(view);
  renderEventActions(view);
}

function renderSeriesSection(view) {
  const section = document.querySelector("#event-dialog-series");
  const series = view?.series;
  section.hidden = !series;
  if (!series) return;
  const { event } = view;
  const startKey = occurrenceDayKey(event);
  document.querySelector("#event-series-summary").textContent = event.recurrence_rule
    ? describeRecurrence(event.recurrence_rule, startKey)
    : "On selected dates";

  const rows = [metaRow("Starts", formatDate(dateFromKey(startKey), {
    weekday: "short",
    month: "short",
    day: "numeric",
    year: "numeric",
  }))];
  const dates = event.recurrence_dates || [];
  const added = dates.filter((item) => item.kind === "include");
  const skipped = dates.filter((item) => item.kind === "exclude");
  if (added.length) rows.push(metaRow("Added", formatLocalDates(added)));
  if (skipped.length) rows.push(metaRow("Skipped", formatLocalDates(skipped)));
  document.querySelector("#event-series-meta").replaceChildren(...rows);

  const selected = view.timingLabel === "When" ? view.timing : null;
  const previous = document.querySelector("#series-previous");
  const next = document.querySelector("#series-next");
  previous.parentElement.hidden = !selected;
  previous.disabled = !series.previous;
  next.disabled = !series.next;
  previous.onclick = () => series.previous && selectSeriesOccurrence(series.previous);
  next.onclick = () => series.next && selectSeriesOccurrence(series.next);
  previous.setAttribute("aria-label", series.previous
    ? `Previous date: ${formatOccurrenceLabel(series.previous)}`
    : "No earlier date");
  next.setAttribute("aria-label", series.next
    ? `Next date: ${formatOccurrenceLabel(series.next)}`
    : "No later date");

  const upcoming = document.querySelector("#event-series-upcoming");
  const currentId = selected?.occurrence_id || (view.timingLabel === "Next date" ? view.timing.occurrence_id : null);
  upcoming.replaceChildren(...series.upcoming.map((occurrence) => {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "series-date";
    const isCurrent = occurrence.occurrence_id === currentId;
    button.classList.toggle("is-current", isCurrent);
    if (isCurrent) button.setAttribute("aria-current", "date");
    button.textContent = formatOccurrenceLabel(occurrence);
    if (occurrence.is_exception) {
      const tag = document.createElement("span");
      tag.className = "series-date-tag";
      tag.textContent = "Added";
      button.append(tag);
    }
    button.addEventListener("click", () => selectSeriesOccurrence(occurrence));
    item.append(button);
    return item;
  }));

  const more = document.querySelector("#event-series-more");
  const remaining = series.upcoming_count - series.upcoming.length;
  const coverageEnd = series.coverage_end
    ? formatDate(addDays(dateFromKey(String(series.coverage_end).slice(0, 10)), -1), {
      month: "short",
      day: "numeric",
      year: "numeric",
    })
    : null;
  const notes = [];
  if (series.upcoming.length === 0) notes.push("No upcoming dates are scheduled.");
  else if (remaining > 0) {
    notes.push(`+${remaining} more date${remaining === 1 ? "" : "s"}${coverageEnd ? ` scheduled through ${coverageEnd}` : ""}.`);
  }
  if (isAdminSignedIn() || creatorToken(event.event_id)) {
    notes.push("Edits apply to every date in this series.");
  }
  more.textContent = notes.join(" ");
}

function renderAdminPanel(view) {
  const panel = document.querySelector("#event-dialog-admin");
  const pending = document.querySelector("#event-admin-pending");
  const admin = view.admin;
  panel.hidden = !isAdminSignedIn() || !(admin || view.adminError);
  pending.hidden = true;
  if (panel.hidden) return;
  if (!admin) {
    document.querySelector("#event-admin-meta").replaceChildren(metaRow("Status", view.adminError));
    return;
  }
  const revisions = admin.revisions || [];
  const published = revisions.find((item) => item.id === admin.published_revision_id);
  const current = revisions.find((item) => item.id === admin.current_revision_id);
  const submitter = admin.original_submitter_contact
    ? `${admin.original_submitter_name} · ${admin.original_submitter_channel}: ${admin.original_submitter_contact}`
    : admin.original_submitter_name;
  const rows = [metaRow("Submitted", `${submitter}\n${formatSubmittedAt(admin.submitted_at)}`)];
  if (published) {
    const reviewer = published.reviewed_by ? ` by ${published.reviewed_by}` : "";
    const at = published.reviewed_at ? ` · ${formatSubmittedAt(published.reviewed_at)}` : "";
    rows.push(metaRow("Published", `Revision ${published.revision_number}, approved${reviewer}${at}`));
  } else {
    rows.push(metaRow("Published", "Not currently published"));
  }
  rows.push(metaRow("Revisions", String(revisions.length)));
  document.querySelector("#event-admin-meta").replaceChildren(...rows);
  if (current && current.id !== admin.published_revision_id && current.approval_status === "pending") {
    pending.hidden = false;
    pending.textContent =
      `Revision ${current.revision_number} from ${current.submitted_by_name} (${formatSubmittedAt(current.submitted_at)}) ` +
      "is awaiting review. Viewers see the published version until it is approved.";
  }
}

function renderEventActions(view) {
  const eventId = eventDetail.eventId;
  const available = Boolean(view.event) || (view.loading && Boolean(eventId));
  const admin = isAdminSignedIn();
  const copy = document.querySelector("#copy-event-link-button");
  const edit = document.querySelector("#edit-event-button");
  const review = document.querySelector("#review-event-edit-button");
  const unpublish = document.querySelector("#unpublish-event-button");
  const cancel = document.querySelector("#cancel-event-button");
  const remove = document.querySelector("#delete-event-button");
  const canRemove = admin || creatorToken(eventId);
  copy.hidden = !available;
  copy.textContent = "Copy link";
  document.querySelector("#event-share-fallback").hidden = true;
  edit.hidden = !available || !canRemove;
  unpublish.hidden = !available || !admin;
  cancel.hidden = !available || !canRemove || Boolean(view.event?.is_cancelled);
  remove.hidden = !available || !canRemove;
  review.hidden = !admin || document.querySelector("#event-admin-pending").hidden;
  copy.parentElement.hidden = [copy, edit, review, unpublish, cancel, remove].every((button) => button.hidden);
}

function currentEventShareUrl() {
  const { view } = eventDetail;
  const timing = view?.timing;
  // While a shared link is still loading, keep the occurrence it named.
  const occurrenceId = view?.timingLabel === "When"
    ? timing?.occurrence_id || eventDetail.occurrenceId
    : (view?.loading && !timing ? eventDetail.occurrenceId : null);
  const dayKey = timing && (timing.starts_at || timing.start_date) ? occurrenceDayKey(timing) : null;
  return eventShareUrl(eventDetail.eventId, occurrenceId, dayKey);
}

async function copyEventLink() {
  const button = document.querySelector("#copy-event-link-button");
  const shareUrl = currentEventShareUrl();
  try {
    await navigator.clipboard.writeText(shareUrl);
    button.textContent = "Link copied";
  } catch (_) {
    const fallback = document.querySelector("#event-share-fallback");
    const input = document.querySelector("#event-share-url");
    input.value = shareUrl;
    fallback.hidden = false;
    input.focus();
    input.select();
  }
}

function editEventFromDetail() {
  const { eventId } = eventDetail;
  eventDialog.close();
  openEditEventForm(eventId);
}

function reviewEventFromDetail() {
  const { eventId } = eventDetail;
  eventDialog.close();
  openReviewDetail(eventId);
}

async function unpublishEventFromDetail() {
  const { eventId, view } = eventDetail;
  const title = view?.event?.title || "this event";
  const warning = `Unpublish "${title}"? It will be removed from the calendar for everyone, and its upcoming dates will be cancelled.`;
  if (!isAdminSignedIn() || !window.confirm(warning)) return;
  const button = document.querySelector("#unpublish-event-button");
  button.disabled = true;
  setEventDetailError("");
  try {
    await jsonRequest(`/api/v1/admin/events/${eventId}/revoke`, {
      method: "POST",
      headers: { ...reviewQueueHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify({ actor: adminDisplayName(), note: "Unpublished from event details" }),
    });
    eventDialog.close();
    await loadMonth();
    statusRegion.textContent = `“${title}” was unpublished.`;
  } catch (error) {
    const message = handleReviewAuthError(error) || error.message;
    // An expired session falls back to the viewer's version of the page.
    if (!isAdminSignedIn() && eventDetail.view) renderEventDetail(eventDetail.view);
    setEventDetailError(message);
  } finally {
    button.disabled = false;
  }
}

function removalHeaders() {
  // Admins authenticate as themselves; creators use their event token.
  // Either credential authorizes cancellation and deletion.
  return {
    ...authenticatedHeaders(creatorToken(eventDetail.eventId)),
    "Content-Type": "application/json",
  };
}

function finishRemoval(button, title, pastTense) {
  eventDialog.close();
  return loadMonth().then(() => {
    statusRegion.textContent = `“${title}” was ${pastTense}.`;
  }).finally(() => {
    button.disabled = false;
  });
}

function failRemoval(error) {
  const message = handleReviewAuthError(error) || error.message;
  // An expired session falls back to the viewer's version of the page.
  if (!isAdminSignedIn() && eventDetail.view) renderEventDetail(eventDetail.view);
  setEventDetailError(message);
}

async function cancelEventFromDetail() {
  const { eventId, view } = eventDetail;
  if (!(isAdminSignedIn() || creatorToken(eventId))) return;
  const title = view?.event?.title || "this event";
  const warning = `Cancel "${title}"? It will stay on the calendar marked as cancelled.`;
  if (!window.confirm(warning)) return;
  const button = document.querySelector("#cancel-event-button");
  button.disabled = true;
  setEventDetailError("");
  try {
    await jsonRequest(`/api/v1/events/${eventId}/cancel`, {
      method: "POST",
      headers: removalHeaders(),
    });
    await finishRemoval(button, title, "cancelled");
  } catch (error) {
    button.disabled = false;
    failRemoval(error);
  }
}

async function deleteEventFromDetail() {
  const { eventId, view } = eventDetail;
  if (!(isAdminSignedIn() || creatorToken(eventId))) return;
  const title = view?.event?.title || "this event";
  const warning = `Delete "${title}"? It will be permanently removed from the calendar for everyone.`;
  if (!window.confirm(warning)) return;
  const button = document.querySelector("#delete-event-button");
  button.disabled = true;
  setEventDetailError("");
  try {
    await jsonRequest(`/api/v1/events/${eventId}`, {
      method: "DELETE",
      headers: removalHeaders(),
    });
    await finishRemoval(button, title, "deleted");
  } catch (error) {
    button.disabled = false;
    failRemoval(error);
  }
}

function closeEventDetail() {
  if (eventDetail.request) eventDetail.request.abort();
  Object.assign(eventDetail, { eventId: null, occurrenceId: null, request: null, view: null });
  writeEventUrl();
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
      state.anchorDate = utcDate(state.jumpYear, month, 1);
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

const eventFormDialog = document.querySelector("#event-form-dialog");
const eventForm = document.querySelector("#event-form");
const eventFormSuccess = document.querySelector("#event-form-success");
const adminDialog = document.querySelector("#admin-dialog");
const reviewQueueDialog = document.querySelector("#review-queue-dialog");
const reviewQueueList = document.querySelector("#review-queue-list");
const reviewQueueError = document.querySelector("#review-queue-error");
const reviewDetailDialog = document.querySelector("#review-detail-dialog");
let eventFormMode = { eventId: null, managementToken: null, stayPending: false };
let reviewQueueItems = [];
let reviewDetail = null;
let availableGroups = [];

function safeSessionGet(key) {
  try {
    return window.sessionStorage.getItem(key);
  } catch (_) {
    return null;
  }
}

function safeSessionSet(key, value) {
  try {
    window.sessionStorage.setItem(key, value);
  } catch (_) {
    // Storage can be unavailable in privacy modes; the current form still works.
  }
}

function safeSessionRemove(key) {
  try {
    window.sessionStorage.removeItem(key);
  } catch (_) {
    // Nothing else is needed when storage is unavailable.
  }
}

function adminToken() {
  return safeSessionGet("calendar-admin-token");
}

function isAdminSignedIn() {
  return Boolean(adminToken());
}

function creatorToken(eventId) {
  return safeSessionGet(`event-management:${eventId}`);
}

function rememberCreatorToken(eventId, token) {
  if (eventId && token) safeSessionSet(`event-management:${eventId}`, token);
}

function authenticatedHeaders(managementToken = null) {
  const headers = { Accept: "application/json" };
  if (adminToken()) headers.Authorization = `Bearer ${adminToken()}`;
  else if (managementToken) headers["X-Event-Management-Token"] = managementToken;
  return headers;
}

function urlWithAccessToken(path) {
  const url = new URL(path, window.location.origin);
  const token = accessToken();
  if (token) url.searchParams.set("token", token);
  return `${url.pathname}${url.search}`;
}

function apiErrorMessage(body, fallback) {
  if (body?.error?.message) return body.error.message;
  if (Array.isArray(body?.detail)) {
    return body.detail.map((item) => item.msg || "Invalid value").join("; ");
  }
  return fallback;
}

async function jsonRequest(path, options = {}) {
  const response = await fetch(path, options);
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const error = new Error(apiErrorMessage(body, `Request failed (${response.status})`));
    error.status = response.status;
    throw error;
  }
  return body;
}

function addIsoDays(value, days) {
  const parsed = dateFromKey(value);
  return dateKey(addDays(parsed, days));
}

function timezoneIsValid(value) {
  try {
    new Intl.DateTimeFormat("en", { timeZone: value }).format();
    return true;
  } catch (_) {
    return false;
  }
}

function timezoneOptions(selected) {
  const fallback = [
    "UTC",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "Europe/London",
    "Europe/Paris",
    "Asia/Tokyo",
    "Australia/Sydney",
  ];
  let names = fallback;
  try {
    if (typeof Intl.supportedValuesOf === "function") {
      names = Intl.supportedValuesOf("timeZone");
    }
  } catch (_) {
    names = fallback;
  }
  names = [...new Set(["UTC", state.timezone, selected, ...names].filter(Boolean))]
    .sort((left, right) => left.localeCompare(right));
  const select = document.querySelector("#event-timezone");
  select.replaceChildren(...names.map((name) => {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name.replaceAll("_", " ");
    return option;
  }));
  select.value = selected || state.timezone || "UTC";
}

function setRecurrenceRule(rule) {
  const select = document.querySelector("#event-recurrence-rule");
  const value = rule || "";
  for (const option of select.querySelectorAll("option[data-custom]")) option.remove();
  if (value && ![...select.options].some((option) => option.value === value)) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = "Current custom schedule";
    option.dataset.custom = "true";
    select.append(option);
  }
  select.value = value;
}

function zonedLocalToIso(value, timezone) {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value)) {
    throw new Error("Start and end must include a date and time.");
  }
  const [day, clock] = value.split("T");
  const [year, month, date] = day.split("-").map(Number);
  const [hour, minute] = clock.split(":").map(Number);
  const wallTimeAsUtc = Date.UTC(year, month - 1, date, hour, minute);
  const formatter = new Intl.DateTimeFormat("en-CA", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  });
  const zoneParts = Object.fromEntries(
    formatter.formatToParts(new Date(wallTimeAsUtc))
      .filter((item) => item.type !== "literal")
      .map((item) => [item.type, Number(item.value)])
  );
  const represented = Date.UTC(
    zoneParts.year,
    zoneParts.month - 1,
    zoneParts.day,
    zoneParts.hour,
    zoneParts.minute,
    zoneParts.second
  );
  let instant = wallTimeAsUtc - (represented - wallTimeAsUtc);

  // One refinement handles offsets on the other side of a DST transition.
  const refinedParts = Object.fromEntries(
    formatter.formatToParts(new Date(instant))
      .filter((item) => item.type !== "literal")
      .map((item) => [item.type, Number(item.value)])
  );
  const refined = Date.UTC(
    refinedParts.year,
    refinedParts.month - 1,
    refinedParts.day,
    refinedParts.hour,
    refinedParts.minute,
    refinedParts.second
  );
  instant -= refined - wallTimeAsUtc;

  const check = Object.fromEntries(
    formatter.formatToParts(new Date(instant))
      .filter((item) => item.type !== "literal")
      .map((item) => [item.type, String(item.value).padStart(2, "0")])
  );
  const roundTrip = `${check.year}-${check.month}-${check.day}T${check.hour}:${check.minute}`;
  if (roundTrip !== value) {
    throw new Error(`${value} does not exist in ${timezone} because of a clock change.`);
  }
  return new Date(instant).toISOString();
}

function isoToZonedInput(value, timezone) {
  if (!value) return "";
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      timeZone: timezone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    }).formatToParts(new Date(value))
      .filter((item) => item.type !== "literal")
      .map((item) => [item.type, item.value])
  );
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
}

function setEventFormError(message) {
  const summary = document.querySelector("#event-form-errors");
  summary.textContent = message || "";
  if (message) summary.focus();
}

function syncTimingFields() {
  const allDay = document.querySelector("#event-all-day").checked;
  document.querySelector("#timed-fields").hidden = allDay;
  document.querySelector("#all-day-fields").hidden = !allDay;
  for (const row of document.querySelectorAll(".recurrence-date-row")) {
    const input = row.querySelector("input");
    const original = input.value;
    input.type = allDay ? "date" : "datetime-local";
    if (allDay && original) input.value = original.slice(0, 10);
    if (!allDay && original && !original.includes("T")) input.value = `${original}T09:00`;
  }
}

function addRecurrenceDate(value = "", kind = "include") {
  const row = document.createElement("div");
  row.className = "recurrence-date-row";
  const dateInput = document.createElement("input");
  dateInput.type = document.querySelector("#event-all-day").checked ? "date" : "datetime-local";
  dateInput.setAttribute("aria-label", "Recurrence date");
  dateInput.value = value ? value.slice(0, dateInput.type === "date" ? 10 : 16) : "";
  const kindInput = document.createElement("select");
  kindInput.setAttribute("aria-label", "Include or exclude date");
  kindInput.innerHTML = '<option value="include">Include</option><option value="exclude">Exclude</option>';
  kindInput.value = kind;
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "remove-date";
  remove.setAttribute("aria-label", "Remove recurrence date");
  remove.textContent = "×";
  remove.addEventListener("click", () => row.remove());
  row.append(dateInput, kindInput, remove);
  document.querySelector("#recurrence-date-list").append(row);
}

async function loadEventGroups(selectedIds = []) {
  const container = document.querySelector("#event-groups");
  const showState = (message, isError = false) => {
    const stateMessage = document.createElement("p");
    stateMessage.className = `group-options-state${isError ? " is-error" : ""}`;
    stateMessage.textContent = message;
    container.replaceChildren(stateMessage);
  };
  showState("Loading groups…");
  try {
    const body = await jsonRequest(urlWithAccessToken("/api/v1/groups"), {
      headers: { Accept: "application/json" },
    });
    availableGroups = body.items;
    const selected = new Set(selectedIds.map(String));
    const options = availableGroups.map((group) => {
      const label = document.createElement("label");
      label.className = "group-option";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = group.id;
      input.checked = selected.has(String(group.id));
      const text = document.createElement("span");
      text.textContent = group.name;
      label.append(input, text);
      return label;
    });
    if (options.length) container.replaceChildren(...options);
    else showState("No groups are available yet.");
  } catch (error) {
    showState(error.message, true);
  }
}

function setDefaultEventTimes() {
  const tomorrow = new Date(Date.now() + DAY_MS);
  const localDay = [
    tomorrow.getFullYear(),
    String(tomorrow.getMonth() + 1).padStart(2, "0"),
    String(tomorrow.getDate()).padStart(2, "0"),
  ].join("-");
  document.querySelector("#event-starts-at").value = `${localDay}T18:00`;
  document.querySelector("#event-ends-at").value = `${localDay}T19:00`;
  document.querySelector("#event-start-date").value = localDay;
  document.querySelector("#event-end-date").value = localDay;
}

function resetEventForm() {
  eventForm.reset();
  document.querySelector("#event-form-id").value = "";
  document.querySelector("#recurrence-date-list").replaceChildren();
  timezoneOptions(state.timezone);
  setRecurrenceRule("");
  document.querySelector("#submitter-channel").value = "email";
  updateSubmitterContactType();
  document.querySelector("#copy-management-link").textContent = "Copy edit link";
  setDefaultEventTimes();
  syncTimingFields();
  setEventFormError("");
  eventForm.hidden = false;
  eventFormSuccess.hidden = true;
}

function updateEventFormMode() {
  const editing = Boolean(eventFormMode.eventId);
  const admin = isAdminSignedIn();
  const queueEdit = editing && admin && eventFormMode.stayPending;
  document.querySelector("#event-form-title").textContent = editing ? "Edit event" : "Add an event";
  document.querySelector("#event-form-eyebrow").textContent = admin ? "Admin event editor" : "Community submission";
  document.querySelector("#event-form-intro").textContent = queueEdit
    ? "Edit this submission. It stays in the review queue until you approve it."
    : admin
      ? "As an admin, this event will be approved and published when you save it."
      : "Submissions and edits are reviewed by an admin before they appear on the calendar.";
  document.querySelector("#approval-note").textContent = queueEdit
    ? "Saving keeps this submission in the review queue."
    : admin
      ? "This admin-authored event will publish immediately."
      : "An admin will review this event before it is published.";
  document.querySelector("#event-submit-button").textContent = queueEdit
    ? "Save edit"
    : admin
      ? (editing ? "Save and publish" : "Publish event")
      : (editing ? "Submit edit for review" : "Submit for review");
}

async function openCreateEventForm() {
  eventFormMode = { eventId: null, managementToken: null, stayPending: false };
  resetEventForm();
  updateEventFormMode();
  eventFormDialog.showModal();
  await loadEventGroups();
}

function populateEventForm(event) {
  document.querySelector("#event-form-id").value = event.event_id;
  document.querySelector("#event-title").value = event.title || "";
  document.querySelector("#event-description").value = event.description || "";
  document.querySelector("#event-location-name").value = event.location_name || "";
  document.querySelector("#event-location-address").value = event.location_address || "";
  document.querySelector("#event-url").value = event.event_url || "";
  document.querySelector("#event-all-day").checked = event.is_all_day;
  timezoneOptions(event.timezone || state.timezone);
  if (event.is_all_day) {
    document.querySelector("#event-start-date").value = event.start_date;
    document.querySelector("#event-end-date").value = addIsoDays(event.end_date, -1);
  } else {
    document.querySelector("#event-starts-at").value = isoToZonedInput(event.starts_at, event.timezone);
    document.querySelector("#event-ends-at").value = isoToZonedInput(event.ends_at, event.timezone);
  }
  setRecurrenceRule(event.recurrence_rule);
  document.querySelector("#submitter-name").value = event.submitted_by_name || "";
  document.querySelector("#submitter-channel").value = event.submitted_by_channel || "email";
  updateSubmitterContactType();
  document.querySelector("#submitter-contact").value = event.submitted_by_contact || "";
  syncTimingFields();
  document.querySelector("#recurrence-date-list").replaceChildren();
  for (const item of event.recurrence_dates || []) addRecurrenceDate(item.local_start, item.kind);
}

async function openEditEventForm(eventId, suppliedToken = null, options = {}) {
  const managementToken = suppliedToken || creatorToken(eventId);
  eventFormMode = {
    eventId,
    managementToken,
    stayPending: Boolean(options.stayPending) && isAdminSignedIn(),
  };
  resetEventForm();
  updateEventFormMode();
  eventFormDialog.showModal();
  try {
    const event = await jsonRequest(`/api/v1/events/${eventId}/manage`, {
      headers: authenticatedHeaders(managementToken),
    });
    populateEventForm(event);
    await loadEventGroups((event.groups || []).map((group) => group.id));
  } catch (error) {
    setEventFormError(error.message);
  }
}

function collectRecurrenceDates(allDay) {
  const result = [];
  for (const row of document.querySelectorAll(".recurrence-date-row")) {
    const value = row.querySelector("input").value;
    if (!value) continue;
    result.push({
      local_start: allDay ? `${value}T00:00:00` : `${value}:00`,
      kind: row.querySelector("select").value,
    });
  }
  return result;
}

function buildEventPayload() {
  const title = document.querySelector("#event-title").value.trim();
  const timezone = document.querySelector("#event-timezone").value.trim();
  const allDay = document.querySelector("#event-all-day").checked;
  const groupIds = [...document.querySelectorAll("#event-groups input:checked")]
    .map((input) => input.value);
  const submitterName = document.querySelector("#submitter-name").value.trim();
  const submitterContact = document.querySelector("#submitter-contact").value.trim();
  if (!title) throw new Error("Title is required.");
  if (!timezone || !timezoneIsValid(timezone)) throw new Error("Enter a valid IANA timezone.");
  if (!submitterName) throw new Error("Your name is required.");

  const payload = {
    title,
    description: document.querySelector("#event-description").value,
    location_name: document.querySelector("#event-location-name").value.trim() || null,
    location_address: document.querySelector("#event-location-address").value.trim() || null,
    event_url: document.querySelector("#event-url").value.trim() || null,
    is_all_day: allDay,
    timezone,
    recurrence_rule: document.querySelector("#event-recurrence-rule").value.trim() || null,
    recurrence_dates: collectRecurrenceDates(allDay),
    group_ids: groupIds,
    submitter: {
      name: submitterName,
      channel: document.querySelector("#submitter-channel").value,
      contact: submitterContact,
    },
  };

  if (payload.event_url) {
    let url;
    try {
      url = new URL(payload.event_url);
    } catch (_) {
      throw new Error("Event website must be a complete http(s) URL.");
    }
    if (!["http:", "https:"].includes(url.protocol)) {
      throw new Error("Event website must be a complete http(s) URL.");
    }
  }

  if (allDay) {
    const start = document.querySelector("#event-start-date").value;
    const lastDay = document.querySelector("#event-end-date").value;
    if (!start || !lastDay) throw new Error("First and last day are required.");
    if (lastDay < start) throw new Error("Last day cannot be before the first day.");
    const duration = (dateFromKey(lastDay) - dateFromKey(start)) / DAY_MS + 1;
    if (duration > 366) throw new Error("All-day events cannot last more than 366 days.");
    payload.start_date = start;
    payload.end_date = addIsoDays(lastDay, 1);
  } else {
    const starts = document.querySelector("#event-starts-at").value;
    const ends = document.querySelector("#event-ends-at").value;
    payload.starts_at = zonedLocalToIso(starts, timezone);
    payload.ends_at = zonedLocalToIso(ends, timezone);
    const duration = Date.parse(payload.ends_at) - Date.parse(payload.starts_at);
    if (duration <= 0) throw new Error("End must be after start.");
    if (duration > 7 * DAY_MS) throw new Error("Timed events cannot last more than 7 days.");
  }
  return payload;
}

function managementLink(eventId, token) {
  const url = new URL(window.location.href);
  for (const key of ["view", "date", "event", "occurrence"]) url.searchParams.delete(key);
  url.hash = `manage=${eventId}.${token}`;
  return url.href;
}

function showEventSuccess(result, token) {
  const approved = result.approval_status === "approved";
  document.querySelector("#event-success-title").textContent = approved ? "Event published" : "Event submitted";
  document.querySelector("#event-success-message").textContent = approved
    ? "The event is approved and now appears on the calendar."
    : "The event is awaiting admin approval. Approved events will appear on the calendar.";
  const linkWrap = document.querySelector("#management-link-wrap");
  linkWrap.hidden = !token;
  if (token) document.querySelector("#management-link").value = managementLink(result.event_id, token);
  eventForm.hidden = true;
  eventFormSuccess.hidden = false;
}

async function submitEventForm(event) {
  event.preventDefault();
  setEventFormError("");
  const submit = document.querySelector("#event-submit-button");
  try {
    const payload = buildEventPayload();
    const editing = Boolean(eventFormMode.eventId);
    const admin = isAdminSignedIn();
    // An admin editing a queued submission keeps it pending so it can still
    // be approved or rejected afterwards; every other admin save publishes.
    const queueEdit = editing && admin && eventFormMode.stayPending;
    let path;
    if (queueEdit) {
      path = `/api/v1/events/${eventFormMode.eventId}/revisions`;
    } else if (admin) {
      path = editing
        ? `/api/v1/admin/events/${eventFormMode.eventId}/revisions`
        : "/api/v1/admin/events";
    } else {
      path = editing
        ? `/api/v1/events/${eventFormMode.eventId}/revisions`
        : urlWithAccessToken("/api/v1/events");
    }
    submit.disabled = true;
    submit.textContent = "Saving…";
    const headers = authenticatedHeaders(eventFormMode.managementToken);
    headers["Content-Type"] = "application/json";
    const result = await jsonRequest(path, {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
    });
    const token = result.management_token || eventFormMode.managementToken;
    rememberCreatorToken(result.event_id, token);
    showEventSuccess(result, token);
    if (queueEdit) {
      await refreshReviewQueue();
      if (reviewDetail && reviewDetail.eventId === result.event_id) {
        await loadReviewDetail(result.event_id);
      }
    } else if (admin) {
      loadMonth();
    }
  } catch (error) {
    if (error.status === 401 && isAdminSignedIn()) {
      signOutAdmin();
    }
    setEventFormError(error.message);
  } finally {
    submit.disabled = false;
    updateEventFormMode();
  }
}

function updateSubmitterContactType() {
  const sms = document.querySelector("#submitter-channel").value === "sms";
  const input = document.querySelector("#submitter-contact");
  document.querySelector("#submitter-contact-label").textContent = sms ? "Mobile number" : "Email address";
  input.type = sms ? "tel" : "email";
  input.placeholder = sms ? "+12125550123" : "name@example.org";
}

function updateAdminUi() {
  const signedIn = isAdminSignedIn();
  const name = safeSessionGet("calendar-admin-name") || "admin";
  document.querySelector("#admin-button").textContent = signedIn ? `Admin: ${name}` : "Admin sign in";
  document.querySelector("#admin-login-form").hidden = signedIn;
  document.querySelector("#admin-signed-in").hidden = !signedIn;
  document.querySelector("#admin-name").textContent = name;
  if (signedIn) {
    updateReviewQueueButton();
    refreshReviewQueue();
  } else {
    reviewQueueItems = [];
    updateReviewQueueButton();
  }
}

async function adminLogin(event) {
  event.preventDefault();
  const error = document.querySelector("#admin-error");
  error.textContent = "";
  try {
    const result = await jsonRequest("/api/v1/admin/login", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({
        username: document.querySelector("#admin-username").value,
        password: document.querySelector("#admin-password").value,
      }),
    });
    safeSessionSet("calendar-admin-token", result.token);
    safeSessionSet("calendar-admin-name", result.username);
    document.querySelector("#admin-password").value = "";
    updateAdminUi();
  } catch (loginError) {
    error.textContent = loginError.message;
  }
}

async function adminLogout() {
  const token = adminToken();
  if (token) {
    await fetch("/api/v1/admin/logout", {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
    }).catch(() => null);
  }
  signOutAdmin();
}

function signOutAdmin() {
  safeSessionRemove("calendar-admin-token");
  safeSessionRemove("calendar-admin-name");
  reviewQueueItems = [];
  reviewDetail = null;
  if (reviewQueueDialog.open) reviewQueueDialog.close();
  if (reviewDetailDialog.open) reviewDetailDialog.close();
  updateAdminUi();
}

function adminDisplayName() {
  return safeSessionGet("calendar-admin-name") || "admin";
}

function reviewQueueHeaders() {
  return { Accept: "application/json", Authorization: `Bearer ${adminToken()}` };
}

function formatSubmittedAt(value) {
  if (!value) return "Unknown date";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function submitterLine(item) {
  const name = item.submitted_by_name || "Anonymous";
  const contact = item.submitted_by_contact
    ? ` · ${item.submitted_by_channel}: ${item.submitted_by_contact}`
    : "";
  return `${name}${contact}`;
}

function setReviewQueueError(message) {
  reviewQueueError.textContent = message || "";
}

function setReviewDetailError(message) {
  document.querySelector("#review-detail-error").textContent = message || "";
}

function handleReviewAuthError(error) {
  if (error && error.status === 401 && isAdminSignedIn()) {
    signOutAdmin();
    return "Your admin session expired. Sign in again to continue reviewing.";
  }
  return null;
}

function updateReviewQueueButton() {
  const button = document.querySelector("#review-queue-button");
  const count = document.querySelector("#review-queue-count");
  if (!isAdminSignedIn()) {
    button.hidden = true;
    count.textContent = "";
    button.removeAttribute("aria-label");
    return;
  }
  button.hidden = false;
  const pending = reviewQueueItems.length;
  count.textContent = pending > 0 ? String(pending) : "";
  button.setAttribute(
    "aria-label",
    pending === 1 ? "Review queue, 1 submission pending" : `Review queue, ${pending} submissions pending`
  );
}

async function refreshReviewQueue() {
  if (!isAdminSignedIn()) {
    reviewQueueItems = [];
    updateReviewQueueButton();
    return;
  }
  try {
    const body = await jsonRequest("/api/v1/admin/events?status=pending&limit=200", {
      headers: reviewQueueHeaders(),
    });
    reviewQueueItems = body.items || [];
    setReviewQueueError("");
  } catch (error) {
    const authMessage = handleReviewAuthError(error);
    if (!authMessage) {
      setReviewQueueError(error.message);
      renderReviewQueue();
      return;
    }
    setReviewQueueError(authMessage);
    reviewQueueItems = [];
  }
  updateReviewQueueButton();
  renderReviewQueue();
}

function reviewCard(item) {
  const card = document.createElement("article");
  card.className = "review-card";

  const title = document.createElement("h3");
  title.className = "review-card-title";
  title.textContent = item.title;
  card.append(title);

  const meta = document.createElement("p");
  meta.className = "review-card-meta";
  meta.textContent = formatEventSchedule(item);
  card.append(meta);

  const submitter = document.createElement("p");
  submitter.className = "review-card-meta";
  submitter.textContent = `Submitted by ${submitterLine(item)} · ${formatSubmittedAt(item.submitted_at)}`;
  card.append(submitter);

  if (item.description) {
    const description = document.createElement("p");
    description.className = "review-card-description";
    description.textContent = item.description;
    card.append(description);
  }

  if (item.groups && item.groups.length > 0) {
    const groups = document.createElement("div");
    groups.className = "event-group-list review-card-groups";
    for (const group of item.groups) {
      const pill = document.createElement("span");
      pill.className = "group-pill";
      pill.textContent = group.name;
      groups.append(pill);
    }
    card.append(groups);
  }

  const actions = document.createElement("div");
  actions.className = "review-card-actions";
  const details = document.createElement("button");
  details.type = "button";
  details.className = "quiet-button";
  details.textContent = "Details";
  details.addEventListener("click", () => openReviewDetail(item.event_id));
  const edit = document.createElement("button");
  edit.type = "button";
  edit.className = "quiet-button";
  edit.textContent = "Edit";
  edit.addEventListener("click", () => editReviewSubmission(item.event_id));
  const approve = document.createElement("button");
  approve.type = "button";
  approve.className = "primary-button";
  approve.textContent = "Approve";
  approve.addEventListener("click", () => moderateReview(item.event_id, item.revision_id, "approve"));
  const reject = document.createElement("button");
  reject.type = "button";
  reject.className = "quiet-button";
  reject.textContent = "Reject";
  reject.addEventListener("click", () => {
    if (window.confirm(`Reject "${item.title}"? It will stay off the public calendar.`)) {
      moderateReview(item.event_id, item.revision_id, "reject");
    }
  });
  actions.append(details, edit, approve, reject);
  card.append(actions);
  return card;
}

function renderReviewQueue() {
  if (reviewQueueItems.length === 0) {
    const empty = document.createElement("p");
    empty.className = "review-queue-empty";
    empty.textContent = "No submissions are awaiting review.";
    reviewQueueList.replaceChildren(empty);
    return;
  }
  reviewQueueList.replaceChildren(...reviewQueueItems.map(reviewCard));
}

async function openReviewQueue() {
  if (!isAdminSignedIn()) return;
  setReviewQueueError("");
  reviewQueueList.replaceChildren();
  const loading = document.createElement("p");
  loading.className = "review-queue-empty";
  loading.textContent = "Loading review queue…";
  reviewQueueList.append(loading);
  if (!reviewQueueDialog.open) reviewQueueDialog.showModal();
  await refreshReviewQueue();
}

function currentPendingRevision(detail) {
  return (detail.revisions || []).find(
    (revision) => revision.id === detail.current_revision_id
  );
}

function recurrenceText(revision) {
  const parts = [];
  if (revision.recurrence_rule) parts.push(`Repeats: ${revision.recurrence_rule}`);
  for (const item of revision.recurrence_dates || []) {
    const marker = item.kind === "exclude" ? "−" : "+";
    parts.push(`${marker} ${String(item.local_start).slice(0, 16)} (${item.kind})`);
  }
  return parts.join("\n");
}

async function openReviewDetail(eventId) {
  if (!isAdminSignedIn()) return;
  setReviewDetailError("");
  document.querySelector("#review-note").value = "";
  try {
    await loadReviewDetail(eventId);
  } catch (error) {
    const authMessage = handleReviewAuthError(error);
    setReviewDetailError(authMessage || error.message);
    return;
  }
  if (!reviewDetailDialog.open) reviewDetailDialog.showModal();
}

async function loadReviewDetail(eventId) {
  const detail = await jsonRequest(`/api/v1/admin/events/${eventId}`, {
    headers: reviewQueueHeaders(),
  });
  const revision = currentPendingRevision(detail);
  if (!revision) throw new Error("This submission has no pending revision.");
  reviewDetail = { eventId, revisionId: revision.id };
  renderReviewDetail(detail, revision);
  setReviewDetailError("");
}

function renderReviewDetail(detail, revision) {
  const groups = document.querySelector("#review-detail-groups");
  groups.replaceChildren(...(revision.groups || []).map((group) => {
    const pill = document.createElement("span");
    pill.className = "group-pill";
    pill.textContent = group.name;
    return pill;
  }));
  document.querySelector("#review-detail-title").textContent = revision.title;
  document.querySelector("#review-detail-submitter").textContent =
    `Submitted by ${submitterLine(revision)} · ${formatSubmittedAt(revision.submitted_at)} · revision ${revision.revision_number}`;
  const rows = [metaRow("When", formatEventSchedule(revision))];
  const location = [revision.location_name, revision.location_address]
    .filter(Boolean)
    .join(" · ");
  if (location) rows.push(metaRow("Where", location));
  if (revision.timezone && !revision.is_all_day) {
    rows.push(metaRow("Timezone", String(revision.timezone).replaceAll("_", " ")));
  }
  const recurrence = recurrenceText(revision);
  if (recurrence) rows.push(metaRow("Recurrence", recurrence));
  rows.push(metaRow("Status", revision.approval_status));
  document.querySelector("#review-detail-meta").replaceChildren(...rows);
  document.querySelector("#review-detail-description").textContent = revision.description || "";
  const link = document.querySelector("#review-detail-link");
  let safeUrl = null;
  try {
    const candidate = new URL(revision.event_url);
    if (["http:", "https:"].includes(candidate.protocol)) safeUrl = candidate.href;
  } catch (_) {
    safeUrl = null;
  }
  link.hidden = !safeUrl;
  if (safeUrl) link.href = safeUrl;

  const history = document.querySelector("#review-detail-history");
  history.replaceChildren();
  const prior = (detail.revisions || []).filter((item) => item.id !== revision.id);
  if (prior.length > 0) {
    const heading = document.createElement("h3");
    heading.className = "review-history-heading";
    heading.textContent = "Earlier revisions";
    history.append(heading);
    for (const item of prior) {
      const entry = document.createElement("p");
      entry.className = "review-history-entry";
      const reviewer = item.reviewed_by ? ` by ${item.reviewed_by}` : "";
      const at = item.reviewed_at ? ` on ${formatSubmittedAt(item.reviewed_at)}` : "";
      const note = item.review_note ? ` — ${item.review_note}` : "";
      entry.textContent =
        `Revision ${item.revision_number} · ${item.approval_status}${reviewer}${at}${note}`;
      history.append(entry);
    }
  }
}

async function moderateReview(eventId, revisionId, action) {
  if (!isAdminSignedIn()) return;
  const note = document.querySelector("#review-note")
    && reviewDetailDialog.open
    && reviewDetail
    && reviewDetail.eventId === eventId
    ? document.querySelector("#review-note").value.trim()
    : "";
  const buttons = [
    ...reviewQueueDialog.querySelectorAll("button"),
    ...reviewDetailDialog.querySelectorAll("button"),
  ];
  for (const button of buttons) button.disabled = true;
  setReviewQueueError("");
  setReviewDetailError("");
  try {
    const body = { actor: adminDisplayName() };
    if (note) body.note = note;
    await jsonRequest(
      `/api/v1/admin/events/${eventId}/revisions/${revisionId}/${action}`,
      { method: "POST", headers: { ...reviewQueueHeaders(), "Content-Type": "application/json" }, body: JSON.stringify(body) }
    );
    if (reviewDetail && reviewDetail.eventId === eventId && reviewDetailDialog.open) {
      reviewDetailDialog.close();
    }
    reviewDetail = null;
    await refreshReviewQueue();
    // An approval publishes new occurrences; reload the calendar so the
    // event appears for every viewer immediately.
    loadMonth();
  } catch (error) {
    const authMessage = handleReviewAuthError(error);
    const message = authMessage || error.message;
    if (reviewDetailDialog.open) setReviewDetailError(message);
    else setReviewQueueError(message);
  } finally {
    for (const button of buttons) button.disabled = false;
  }
}

async function editReviewSubmission(eventId) {
  if (reviewDetailDialog.open) reviewDetailDialog.close();
  await openEditEventForm(eventId, null, { stayPending: true });
}

function consumeManagementHash() {
  const match = window.location.hash.match(/^#manage=([0-9a-f-]{36})\.([A-Za-z0-9_-]+)$/i);
  if (!match) return null;
  const [, eventId, token] = match;
  rememberCreatorToken(eventId, token);
  try {
    const url = new URL(window.location.href);
    url.hash = "";
    window.history.replaceState(null, "", url);
  } catch (_) {
    // Keeping the fragment is safe; fragments are not sent to the server.
  }
  return { eventId, token };
}

function bindControls() {
  const token = accessToken();
  if (token) document.querySelector(".brand").href = `/?token=${encodeURIComponent(token)}`;
  document.querySelector("#previous-month").addEventListener("click", () => moveStep(-1));
  document.querySelector("#next-month").addEventListener("click", () => moveStep(1));
  document.querySelector("#today-button").addEventListener("click", () => {
    state.displayedMonth = utcDate(now.getFullYear(), now.getMonth(), 1);
    state.anchorDate = new Date(Date.UTC(now.getFullYear(), now.getMonth(), now.getDate()));
    loadMonth();
  });
  for (const button of viewSwitcher.querySelectorAll("[data-view]")) {
    button.addEventListener("click", () => setView(button.dataset.view));
  }
  document.querySelector("#month-heading").addEventListener("click", () => {
    state.jumpYear = state.view === "month"
      ? state.displayedMonth.getUTCFullYear()
      : state.anchorDate.getUTCFullYear();
    renderMonthJump();
    monthJump.showModal();
  });
  document.querySelector("#jump-prev-year").addEventListener("click", () => jumpYear(-1));
  document.querySelector("#jump-next-year").addEventListener("click", () => jumpYear(1));
  document.querySelector("#create-event-button").addEventListener("click", openCreateEventForm);
  document.querySelector("#event-all-day").addEventListener("change", syncTimingFields);
  document.querySelector("#add-recurrence-date").addEventListener("click", () => addRecurrenceDate());
  document.querySelector("#submitter-channel").addEventListener("change", updateSubmitterContactType);
  eventForm.addEventListener("submit", submitEventForm);
  document.querySelector("#event-success-close").addEventListener("click", () => eventFormDialog.close());
  document.querySelector("#copy-management-link").addEventListener("click", async () => {
    const input = document.querySelector("#management-link");
    try {
      await navigator.clipboard.writeText(input.value);
      document.querySelector("#copy-management-link").textContent = "Copied";
    } catch (_) {
      input.select();
    }
  });
  document.querySelector("#admin-button").addEventListener("click", () => {
    updateAdminUi();
    adminDialog.showModal();
  });
  document.querySelector("#review-queue-button").addEventListener("click", openReviewQueue);
  document.querySelector("#copy-event-link-button").addEventListener("click", copyEventLink);
  document.querySelector("#edit-event-button").addEventListener("click", editEventFromDetail);
  document.querySelector("#review-event-edit-button").addEventListener("click", reviewEventFromDetail);
  document.querySelector("#unpublish-event-button").addEventListener("click", unpublishEventFromDetail);
  document.querySelector("#cancel-event-button").addEventListener("click", cancelEventFromDetail);
  document.querySelector("#delete-event-button").addEventListener("click", deleteEventFromDetail);
  eventDialog.addEventListener("close", closeEventDetail);
  document.querySelector("#review-approve-button").addEventListener("click", () => {
    if (reviewDetail) moderateReview(reviewDetail.eventId, reviewDetail.revisionId, "approve");
  });
  document.querySelector("#review-reject-button").addEventListener("click", () => {
    if (!reviewDetail) return;
    if (window.confirm("Reject this submission? It will stay off the public calendar.")) {
      moderateReview(reviewDetail.eventId, reviewDetail.revisionId, "reject");
    }
  });
  document.querySelector("#review-edit-button").addEventListener("click", () => {
    if (reviewDetail) editReviewSubmission(reviewDetail.eventId);
  });
  document.querySelector("#admin-login-form").addEventListener("submit", adminLogin);
  document.querySelector("#admin-logout").addEventListener("click", adminLogout);
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
    if (event.key === "ArrowLeft" && event.altKey) moveStep(-1);
    if (event.key === "ArrowRight" && event.altKey) moveStep(1);
  });
}

const managementRequest = consumeManagementHash();
bindControls();
updateAdminUi();
renderCalendar();
loadMonth();
if (managementRequest) {
  openEditEventForm(managementRequest.eventId, managementRequest.token);
} else {
  openEventFromUrl();
}
