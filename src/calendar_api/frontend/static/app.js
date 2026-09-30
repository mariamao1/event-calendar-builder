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
      button.className = "allday-chip";
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

bindControls();
renderCalendar();
loadMonth();
