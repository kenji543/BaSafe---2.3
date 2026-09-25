(() => {
  "use strict";

  const page = document.body.dataset.adminPage || "overview";
  const byId = (id) => document.getElementById(id);
  const state = { loading: false, centers: new Map(), barangayRows: new Map(), calendarMonth: null, hazardEventsByDate: new Map(), hazardEventsList: [], hazardView: "calendar", reports: new Map() };

  function text(id, value) {
    const target = byId(id);
    if (target) target.textContent = value ?? "—";
  }

  function element(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined) node.textContent = content;
    return node;
  }

  function svgElement(tag, attributes = {}) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
    return node;
  }

  function installSessionControls() {
    const refresh = byId("refresh-dashboard");
    if (!refresh || refresh.parentElement?.classList.contains("admin-actions")) return;
    const actions = element("div", "admin-actions");
    const logout = element("button", "logout-button");
    logout.id = "logout-admin";
    logout.type = "button";
    const icon = svgElement("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" });
    icon.append(
      svgElement("path", { d: "M10 17l5-5-5-5M15 12H3M21 3v18" })
    );
    logout.append(element("span", "", "Sign out"), icon);
    refresh.replaceWith(actions);
    actions.append(refresh, logout);
    logout.addEventListener("click", async () => {
      logout.disabled = true;
      try {
        await fetch("/api/v1/admin/logout", {
          method: "POST",
          cache: "no-store",
          credentials: "same-origin",
          headers: { Accept: "application/json" }
        });
      } finally {
        window.location.replace("/admin/login?signed_out=1");
      }
    });
  }

  function formatDate(value, includeTime = false) {
    if (!value) return "Not reported";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat(undefined, {
      year: "numeric", month: "short", day: "numeric",
      ...(includeTime ? { hour: "numeric", minute: "2-digit" } : {})
    }).format(date);
  }

  function label(value) {
    return String(value || "unknown").replaceAll("_", " ").replace(/\b\w/g, (character) => character.toUpperCase());
  }

  function statusBadge(value) {
    const supported = ["available", "verified", "complete", "provisional", "limited", "attention", "unavailable", "critical", "incomplete", "unknown"];
    const normalized = supported.includes(value) ? value : "neutral";
    return element("span", `status-badge ${normalized}`, label(value));
  }

  async function apiFetch(url, options = {}) {
    const response = await fetch(url, {
      cache: "no-store",
      headers: { Accept: "application/json", ...(options.headers || {}) },
      ...options
    });
    if (response.status === 401) {
      window.location.replace("/admin/login?expired=1");
      throw new Error("Your administrator session has expired.");
    }
    if (!response.ok) {
      let message = `Admin service returned HTTP ${response.status}.`;
      try { message = (await response.json()).error?.message || message; } catch { /* response was not JSON */ }
      throw new Error(message);
    }
    return response.json();
  }

  function renderSummary(payload) {
    const system = payload.system;
    const centers = payload.evacuation_centers;
    const scoring = payload.scoring;
    const analytics = payload.analytics || {};
    const statusLabels = { healthy: "Healthy", attention: "Needs review", critical: "Action required" };
    text("metric-system", statusLabels[system.status] || label(system.status));
    text("metric-system-detail", system.critical_alerts ? `${system.critical_alerts} critical notice(s)` : system.warnings ? `${system.warnings} item(s) need review` : "Required checks are responding");
    const statusCard = document.querySelector(".status-card");
    if (statusCard) statusCard.dataset.status = system.status;
    text("metric-hazards", `${system.required_hazards_loaded}/${system.required_hazards_total}`);
    text("metric-visitors", analytics.unique_visitors ?? 0);
    text("metric-online", analytics.online_now ?? 0);
    text("metric-centers", centers.active);
    text("metric-centers-detail", `${centers.official} active official-source records`);
    text("metric-scores", scoring.total);
    text("metric-scores-detail", `${scoring.complete} complete · ${scoring.incomplete} incomplete`);
    text("last-updated", `Updated ${formatDate(payload.generated_at, true)} · auto-refreshes every minute`);
  }

  function renderAlerts(alerts) {
    const target = byId("alert-list");
    if (!target) return;
    target.replaceChildren();
    for (const alert of alerts) {
      const item = element("article", `alert-item ${alert.severity}`);
      const icon = element("div", "alert-icon", alert.severity === "critical" ? "!" : alert.severity === "warning" ? "△" : alert.severity === "healthy" ? "✓" : "i");
      icon.setAttribute("aria-hidden", "true");
      const copy = element("div", "alert-copy");
      copy.append(element("strong", "", alert.title), element("span", "", alert.message), element("small", "", alert.action));
      item.append(icon, copy);
      target.append(item);
    }
    const actionable = alerts.filter((alert) => alert.severity !== "healthy").length;
    text("alert-summary", `${actionable} notice${actionable === 1 ? "" : "s"}`);
  }

  function renderDatasets(datasets) {
    const target = byId("dataset-rows");
    if (!target) return;
    target.replaceChildren();
    for (const dataset of datasets) {
      const row = document.createElement("tr");
      const hazardCell = document.createElement("td");
      const primary = element("div", "cell-primary");
      primary.append(element("strong", "", label(dataset.hazard_type)), element("span", "", dataset.name));
      hazardCell.append(primary);
      row.append(hazardCell, element("td", "", dataset.source_name), element("td", "", dataset.feature_count.toLocaleString()), element("td", "", formatDate(dataset.source_date)));
      const statusCell = document.createElement("td");
      statusCell.append(statusBadge(dataset.quality_status));
      row.append(statusCell);
      target.append(row);
    }
  }

  function placeholderPhoto() {
    const container = element("div", "center-photo");
    const icon = svgElement("svg", { viewBox: "0 0 48 48", "aria-hidden": "true" });
    icon.append(svgElement("path", { d: "M7 14h9l3-4h10l3 4h9v25H7V14Z" }), svgElement("circle", { cx: "24", cy: "26", r: "8" }));
    container.append(icon, element("span", "", "No verified photograph"));
    return container;
  }

  function renderCentersState(payload) {
    state.centers.clear();
    for (const center of payload.items || []) state.centers.set(String(center.id), center);
  }

  function buildBarangayCard(row) {
    const card = element("article", "center-admin-card barangay-card");
    card.append(element("h2", "", row.barangay_name));
    const center = row.designated_center;
    const copy = element("div", "center-card-copy");
    if (center) {
      const fullCenter = state.centers.get(String(center.id));
      let media = placeholderPhoto();
      if (fullCenter?.photo_url) {
        media = element("div", "center-photo");
        const image = document.createElement("img");
        image.src = fullCenter.photo_url;
        image.alt = fullCenter.photo_alt || `Photograph of ${center.name}`;
        image.loading = "lazy";
        media.append(image);
      }
      copy.append(element("h3", "", center.name));
      copy.append(element("span", "", `${Number(center.latitude).toFixed(5)}, ${Number(center.longitude).toFixed(5)}`));
      if (center.notes) copy.append(element("p", "barangay-card-notes", center.notes));
      const statusRow = element("div", "barangay-card-status");
      const badge = statusBadge(row.designation_published_at ? "verified" : "neutral");
      badge.textContent = row.designation_published_at ? "Published" : "Draft — not published";
      statusRow.append(badge);
      copy.append(statusRow);
      const actions = element("div", "barangay-card-actions");
      const editButton = element("button", "button-secondary", "Edit");
      editButton.type = "button";
      editButton.dataset.action = "edit";
      editButton.dataset.barangayId = row.barangay_id;
      editButton.dataset.centerId = center.id;
      const changeButton = element("button", "button-secondary", "Change center");
      changeButton.type = "button";
      changeButton.dataset.action = "reassign";
      changeButton.dataset.barangayId = row.barangay_id;
      const photoButton = element("button", "button-secondary", "Photo");
      photoButton.type = "button";
      photoButton.dataset.action = "photo";
      photoButton.dataset.centerId = center.id;
      const publishButton = element("button", "button-primary", row.designation_published_at ? "Re-publish" : "Publish");
      publishButton.type = "button";
      publishButton.dataset.action = "publish";
      publishButton.dataset.barangayId = row.barangay_id;
      publishButton.dataset.centerId = center.id;
      actions.append(editButton, changeButton, photoButton, publishButton);
      copy.append(actions);
      card.append(media, copy);
    } else {
      copy.append(element("p", "barangay-card-empty", "No evacuation center assigned yet."));
      const actions = element("div", "barangay-card-actions");
      const assignButton = element("button", "button-secondary", "Assign existing center");
      assignButton.type = "button";
      assignButton.dataset.action = "assign";
      assignButton.dataset.barangayId = row.barangay_id;
      const createButton = element("button", "button-primary", "Create new center");
      createButton.type = "button";
      createButton.dataset.action = "create";
      createButton.dataset.barangayId = row.barangay_id;
      actions.append(assignButton, createButton);
      copy.append(actions);
      card.append(copy);
    }
    return card;
  }

  function renderBarangayCards(payload) {
    const target = byId("barangay-center-cards");
    if (!target) return;
    target.replaceChildren();
    state.barangayRows = new Map((payload.items || []).map((row) => [String(row.barangay_id), row]));
    for (const row of payload.items || []) target.append(buildBarangayCard(row));
    const total = payload.count || 0;
    const assigned = payload.assigned_count || 0;
    const publishedCount = (payload.items || []).filter((row) => row.designation_published_at).length;
    text("center-count-badge", `${assigned}/${total} barangays assigned`);
    text("barangay-coverage-label", `${assigned}/${total}`);
    text("publish-coverage-label", `${publishedCount}/${assigned}`);
    const assignedPercent = total ? Math.round((assigned / total) * 100) : 0;
    const publishedPercent = assigned ? Math.round((publishedCount / assigned) * 100) : 0;
    if (byId("barangay-coverage-bar")) byId("barangay-coverage-bar").style.width = `${assignedPercent}%`;
    if (byId("publish-coverage-bar")) byId("publish-coverage-bar").style.width = `${publishedPercent}%`;
  }

  function openCenterForm({ mode, barangayId, barangayName, center }) {
    byId("center-form-mode").value = mode;
    byId("center-form-barangay-id").value = barangayId ?? "";
    byId("center-form-center-id").value = center?.id ?? "";
    text("center-form-title", mode === "create" ? "Create evacuation center" : "Edit evacuation center");
    text("center-form-eyebrow", mode === "create" ? "New facility" : "Facility details");
    text("center-form-barangay-name", mode === "create" ? `For ${barangayName}` : "");
    byId("center-form-name").value = center?.name || "";
    byId("center-form-latitude").value = center?.latitude ?? "";
    byId("center-form-longitude").value = center?.longitude ?? "";
    byId("center-form-notes").value = center?.notes || "";
    text("center-form-status", "");
    byId("center-form-status").className = "upload-status";
    byId("center-form-dialog").showModal();
  }

  async function submitCenterForm(event) {
    event.preventDefault();
    const mode = byId("center-form-mode").value;
    const status = byId("center-form-status");
    const submit = byId("submit-center-form");
    const requestBody = {
      name: byId("center-form-name").value,
      latitude: Number(byId("center-form-latitude").value),
      longitude: Number(byId("center-form-longitude").value),
      notes: byId("center-form-notes").value || null,
    };
    submit.disabled = true;
    status.textContent = "Saving…";
    status.className = "upload-status";
    try {
      if (mode === "create") {
        const created = await apiFetch("/api/v1/admin/evacuation-centers", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(requestBody),
        });
        const barangayId = byId("center-form-barangay-id").value;
        await apiFetch(`/api/v1/admin/barangays/${barangayId}/designate`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ evacuation_center_id: created.id }),
        });
      } else {
        const centerId = byId("center-form-center-id").value;
        await apiFetch(`/api/v1/admin/evacuation-centers/${centerId}`, {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(requestBody),
        });
      }
      status.textContent = "Saved as a draft. Publish to update the local public app.";
      status.className = "upload-status success";
      await loadDashboard({ quiet: true });
      window.setTimeout(() => byId("center-form-dialog").close(), 700);
    } catch (error) {
      status.textContent = error.message;
      status.className = "upload-status error";
    } finally {
      submit.disabled = false;
    }
  }

  function openAssignDialog(barangayId, barangayName) {
    byId("assign-center-barangay-id").value = barangayId;
    text("assign-center-barangay-name", `For ${barangayName}`);
    const select = byId("assign-center-select");
    select.replaceChildren();
    for (const center of state.centers.values()) {
      if (!center.active) continue;
      const option = document.createElement("option");
      option.value = center.id;
      option.textContent = `${center.name} (${center.barangay || "barangay not reported"})`;
      select.append(option);
    }
    text("assign-center-status", "");
    byId("assign-center-status").className = "upload-status";
    byId("assign-center-dialog").showModal();
  }

  async function submitAssignForm(event) {
    event.preventDefault();
    const barangayId = byId("assign-center-barangay-id").value;
    const centerId = Number(byId("assign-center-select").value);
    const status = byId("assign-center-status");
    const submit = byId("submit-assign-center");
    submit.disabled = true;
    status.textContent = "Assigning…";
    status.className = "upload-status";
    try {
      await apiFetch(`/api/v1/admin/barangays/${barangayId}/designate`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ evacuation_center_id: centerId }),
      });
      status.textContent = "Assigned as a draft. Publish to update the local public app.";
      status.className = "upload-status success";
      await loadDashboard({ quiet: true });
      window.setTimeout(() => byId("assign-center-dialog").close(), 700);
    } catch (error) {
      status.textContent = error.message;
      status.className = "upload-status error";
    } finally {
      submit.disabled = false;
    }
  }

  async function publishBarangay(barangayId, centerId, button) {
    const originalLabel = button.textContent;
    button.disabled = true;
    button.textContent = "Publishing…";
    try {
      await apiFetch(`/api/v1/admin/evacuation-centers/${centerId}/publish`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}),
      });
      await apiFetch(`/api/v1/admin/barangays/${barangayId}/publish`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}),
      });
      await loadDashboard({ quiet: true });
    } catch (error) {
      button.textContent = "Publish failed";
      button.title = error.message;
      window.setTimeout(() => { button.textContent = originalLabel; button.disabled = false; }, 2500);
    }
  }

  function handleBarangayCardClick(event) {
    const button = event.target.closest("[data-action]");
    if (!button) return;
    const barangayId = button.dataset.barangayId;
    const row = barangayId ? state.barangayRows?.get(String(barangayId)) : null;
    const barangayName = row?.barangay_name || "this barangay";
    if (button.dataset.action === "edit") {
      openCenterForm({ mode: "edit", barangayId, barangayName, center: state.centers.get(String(button.dataset.centerId)) });
    } else if (button.dataset.action === "create") {
      openCenterForm({ mode: "create", barangayId, barangayName });
    } else if (button.dataset.action === "assign" || button.dataset.action === "reassign") {
      openAssignDialog(barangayId, barangayName);
    } else if (button.dataset.action === "photo") {
      openPhotoDialog(button.dataset.centerId);
    } else if (button.dataset.action === "publish") {
      publishBarangay(barangayId, button.dataset.centerId, button);
    }
  }

  function renderOverview(payload) {
    renderSummary(payload);
    renderAlerts(payload.alerts || []);
    renderDatasets(payload.datasets || []);
  }

  function areaPath(points, baselineY) {
    if (!points.length) return "";
    const top = points.map((point, index) => `${index === 0 ? "M" : "L"}${point[0]},${point[1]}`).join(" ");
    const last = points[points.length - 1];
    const first = points[0];
    return `${top} L${last[0]},${baselineY} L${first[0]},${baselineY} Z`;
  }

  function renderChart(series) {
    const svg = byId("visitor-chart");
    if (!svg) return;
    svg.replaceChildren();
    const width = 760, height = 280, left = 46, right = 20, top = 20, bottom = 45;
    const plotWidth = width - left - right, plotHeight = height - top - bottom;
    const baselineY = top + plotHeight;
    svg.append(svgElement("defs", {}));
    const defs = svg.querySelector("defs");
    const visitorsGradient = svgElement("linearGradient", { id: "visitorsAreaGradient", x1: "0", y1: "0", x2: "0", y2: "1" });
    visitorsGradient.append(
      svgElement("stop", { offset: "0%", "stop-color": "#0f9d91", "stop-opacity": ".28" }),
      svgElement("stop", { offset: "100%", "stop-color": "#0f9d91", "stop-opacity": "0" })
    );
    const viewsGradient = svgElement("linearGradient", { id: "viewsAreaGradient", x1: "0", y1: "0", x2: "0", y2: "1" });
    viewsGradient.append(
      svgElement("stop", { offset: "0%", "stop-color": "#6d5bd0", "stop-opacity": ".22" }),
      svgElement("stop", { offset: "100%", "stop-color": "#6d5bd0", "stop-opacity": "0" })
    );
    defs.append(visitorsGradient, viewsGradient);
    if (!series.length) {
      const empty = svgElement("text", { x: width / 2, y: height / 2, class: "chart-empty-label" });
      empty.textContent = "No visitor activity recorded yet.";
      svg.append(empty);
      return;
    }
    const maximum = Math.max(1, ...series.flatMap((item) => [item.page_views, item.unique_visitors]));
    for (let tick = 0; tick <= 4; tick += 1) {
      const y = top + (plotHeight * tick / 4);
      svg.append(svgElement("line", { x1: left, y1: y, x2: width - right, y2: y, class: "chart-grid-line" }));
      const axis = svgElement("text", { x: left - 10, y: y + 4, class: "chart-axis-label", "text-anchor": "end" });
      axis.textContent = Math.round(maximum * (1 - tick / 4));
      svg.append(axis);
    }
    const point = (item, index, field) => {
      const x = left + (series.length === 1 ? plotWidth / 2 : plotWidth * index / (series.length - 1));
      const y = top + plotHeight - (item[field] / maximum) * plotHeight;
      return [x, y];
    };
    const visitors = series.map((item, index) => point(item, index, "unique_visitors"));
    const views = series.map((item, index) => point(item, index, "page_views"));
    svg.append(
      svgElement("path", { d: areaPath(views, baselineY), fill: "url(#viewsAreaGradient)", stroke: "none" }),
      svgElement("path", { d: areaPath(visitors, baselineY), fill: "url(#visitorsAreaGradient)", stroke: "none" })
    );
    const visitorsLine = svgElement("polyline", { points: visitors.map((value) => value.join(",")).join(" "), class: "chart-visitors-line" });
    const viewsLine = svgElement("polyline", { points: views.map((value) => value.join(",")).join(" "), class: "chart-views-line" });
    svg.append(visitorsLine, viewsLine);
    series.forEach((item, index) => {
      const date = new Date(`${item.date}T00:00:00Z`);
      const [visitorX, visitorY] = visitors[index];
      const [viewX, viewY] = views[index];
      const visitorCircle = svgElement("circle", { cx: visitorX, cy: visitorY, r: 4, class: "chart-visitors-point" });
      const visitorTitle = svgElement("title"); visitorTitle.textContent = `${item.date}: ${item.unique_visitors} visitor(s)`; visitorCircle.append(visitorTitle);
      const viewCircle = svgElement("circle", { cx: viewX, cy: viewY, r: 4, class: "chart-views-point" });
      const viewTitle = svgElement("title"); viewTitle.textContent = `${item.date}: ${item.page_views} page view(s)`; viewCircle.append(viewTitle);
      const dayLabel = svgElement("text", { x: visitorX, y: height - 17, class: "chart-axis-label", "text-anchor": "middle" });
      dayLabel.textContent = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", timeZone: "UTC" }).format(date);
      svg.append(visitorCircle, viewCircle, dayLabel);
    });
  }

  function groupEventsByDate(events) {
    const map = new Map();
    for (const item of events) {
      const key = String(item.occurred_at).slice(0, 10);
      if (!map.has(key)) map.set(key, []);
      map.get(key).push(item);
    }
    return map;
  }

  function startOfMonthUtc(date) {
    return new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), 1));
  }

  function renderCalendar() {
    const grid = byId("calendar-grid");
    const weekdaysRow = byId("calendar-weekdays");
    if (!grid || !weekdaysRow || !state.calendarMonth) return;
    const monthDate = state.calendarMonth;
    text("calendar-month-label", new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric", timeZone: "UTC" }).format(monthDate));
    if (!weekdaysRow.childElementCount) {
      for (const name of ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]) weekdaysRow.append(element("span", "", name));
    }
    grid.replaceChildren();
    const year = monthDate.getUTCFullYear();
    const month = monthDate.getUTCMonth();
    const firstWeekday = new Date(Date.UTC(year, month, 1)).getUTCDay();
    const daysInMonth = new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
    const eventsByDate = state.hazardEventsByDate || new Map();
    const todayKey = new Date().toISOString().slice(0, 10);
    for (let i = 0; i < firstWeekday; i += 1) grid.append(element("div", "calendar-day empty"));
    for (let day = 1; day <= daysInMonth; day += 1) {
      const dateKey = `${year}-${String(month + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
      const dayEvents = eventsByDate.get(dateKey) || [];
      const cell = document.createElement("button");
      cell.type = "button";
      cell.className = "calendar-day" + (dayEvents.length ? " has-events" : "") + (dateKey === todayKey ? " today" : "");
      cell.append(element("span", "calendar-day-number", String(day)));
      const longDate = new Intl.DateTimeFormat(undefined, { month: "long", day: "numeric", year: "numeric", timeZone: "UTC" }).format(new Date(`${dateKey}T00:00:00Z`));
      if (dayEvents.length) {
        if (dayEvents.length > 1) cell.append(element("span", "calendar-day-count", `×${dayEvents.length}`));
        const bar = element("span", "calendar-day-bar");
        for (const type of new Set(dayEvents.map((item) => item.event_type))) bar.append(element("i", `event-bar-segment ${type}`));
        cell.append(bar);
        cell.setAttribute("aria-label", `${longDate}: ${dayEvents.length} event${dayEvents.length === 1 ? "" : "s"} logged`);
        cell.addEventListener("click", () => showEventDetail(dateKey));
      } else {
        cell.disabled = true;
        cell.setAttribute("aria-label", `${longDate}: no events logged`);
      }
      grid.append(cell);
    }
  }

  function weeklyRainEarthquakeBuckets(events) {
    if (!events.length) return [];
    const sorted = [...events].sort((a, b) => a.occurred_at.localeCompare(b.occurred_at));
    const firstDay = new Date(`${sorted[0].occurred_at.slice(0, 10)}T00:00:00Z`);
    const buckets = new Map();
    for (const item of sorted) {
      const day = new Date(`${item.occurred_at.slice(0, 10)}T00:00:00Z`);
      const weekIndex = Math.floor((day - firstDay) / (7 * 86400000));
      const weekStart = new Date(firstDay.getTime() + weekIndex * 7 * 86400000).toISOString().slice(0, 10);
      if (!buckets.has(weekStart)) buckets.set(weekStart, { week_start: weekStart, rain_mm: 0, earthquake_max_magnitude: null });
      const bucket = buckets.get(weekStart);
      if (item.event_type === "rain") bucket.rain_mm += item.severity_value;
      else if (item.event_type === "earthquake") {
        bucket.earthquake_max_magnitude = bucket.earthquake_max_magnitude === null
          ? item.severity_value
          : Math.max(bucket.earthquake_max_magnitude, item.severity_value);
      }
    }
    return Array.from(buckets.values()).sort((a, b) => a.week_start.localeCompare(b.week_start));
  }

  function renderHazardTrend(events) {
    const svg = byId("hazard-trend-chart");
    if (!svg) return;
    svg.replaceChildren();
    const width = 760, height = 280, left = 46, right = 46, top = 20, bottom = 45;
    const plotWidth = width - left - right, plotHeight = height - top - bottom;
    const baselineY = top + plotHeight;
    svg.append(svgElement("defs", {}));
    const defs = svg.querySelector("defs");
    const rainGradient = svgElement("linearGradient", { id: "hazardRainAreaGradient", x1: "0", y1: "0", x2: "0", y2: "1" });
    rainGradient.append(
      svgElement("stop", { offset: "0%", "stop-color": "#0f9d91", "stop-opacity": ".28" }),
      svgElement("stop", { offset: "100%", "stop-color": "#0f9d91", "stop-opacity": "0" })
    );
    defs.append(rainGradient);

    const weeks = weeklyRainEarthquakeBuckets(events);
    if (!weeks.length) {
      const empty = svgElement("text", { x: width / 2, y: height / 2, class: "chart-empty-label" });
      empty.textContent = "No hazard events logged yet.";
      svg.append(empty);
      return;
    }

    const maxRain = Math.max(1, ...weeks.map((item) => item.rain_mm));
    const magnitudeWeeks = weeks.filter((item) => item.earthquake_max_magnitude !== null);
    const maxMagnitude = Math.max(1, ...magnitudeWeeks.map((item) => item.earthquake_max_magnitude));

    for (let tick = 0; tick <= 4; tick += 1) {
      const y = top + (plotHeight * tick / 4);
      svg.append(svgElement("line", { x1: left, y1: y, x2: width - right, y2: y, class: "chart-grid-line" }));
      const rainAxis = svgElement("text", { x: left - 10, y: y + 4, class: "chart-axis-label", "text-anchor": "end" });
      rainAxis.textContent = Math.round(maxRain * (1 - tick / 4));
      svg.append(rainAxis);
      const magnitudeAxis = svgElement("text", { x: width - right + 10, y: y + 4, class: "chart-axis-label chart-axis-label-eq", "text-anchor": "start" });
      magnitudeAxis.textContent = (maxMagnitude * (1 - tick / 4)).toFixed(1);
      svg.append(magnitudeAxis);
    }

    const xAt = (index) => left + (weeks.length === 1 ? plotWidth / 2 : plotWidth * index / (weeks.length - 1));
    const rainPoints = weeks.map((item, index) => [xAt(index), top + plotHeight - (item.rain_mm / maxRain) * plotHeight]);

    svg.append(svgElement("path", { d: areaPath(rainPoints, baselineY), fill: "url(#hazardRainAreaGradient)", stroke: "none" }));
    svg.append(svgElement("polyline", { points: rainPoints.map((value) => value.join(",")).join(" "), class: "chart-visitors-line" }));

    weeks.forEach((item, index) => {
      const [x, y] = rainPoints[index];
      const circle = svgElement("circle", { cx: x, cy: y, r: 3.5, class: "chart-visitors-point" });
      const title = svgElement("title");
      title.textContent = `Week of ${item.week_start}: ${item.rain_mm.toFixed(1)}mm rain`;
      circle.append(title);
      svg.append(circle);
    });

    weeks.forEach((item, index) => {
      if (item.earthquake_max_magnitude === null) return;
      const [x] = rainPoints[index];
      const y = top + plotHeight - (item.earthquake_max_magnitude / maxMagnitude) * plotHeight;
      const circle = svgElement("circle", { cx: x, cy: y, r: 5, class: "chart-views-point chart-earthquake-point" });
      const title = svgElement("title");
      title.textContent = `Week of ${item.week_start}: magnitude ${item.earthquake_max_magnitude.toFixed(1)}`;
      circle.append(title);
      svg.append(circle);
    });

    const labelEvery = Math.max(1, Math.ceil(weeks.length / 9));
    weeks.forEach((item, index) => {
      if (index % labelEvery !== 0 && index !== weeks.length - 1) return;
      const [x] = rainPoints[index];
      const weekDate = new Date(`${item.week_start}T00:00:00Z`);
      const label = svgElement("text", { x, y: height - 17, class: "chart-axis-label", "text-anchor": "middle" });
      label.textContent = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", timeZone: "UTC" }).format(weekDate);
      svg.append(label);
    });
  }

  function setHazardView(view) {
    state.hazardView = view;
    const calendarView = byId("hazard-calendar-view");
    const trendView = byId("hazard-trend-view");
    if (calendarView) calendarView.hidden = view !== "calendar";
    if (trendView) trendView.hidden = view !== "trend";
    text("hazard-view-eyebrow", view === "calendar" ? "Occurrence calendar" : "Occurrence trend");
    document.querySelectorAll("[data-hazard-view]").forEach((button) => {
      const active = button.dataset.hazardView === view;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    if (view === "calendar") renderCalendar();
    else renderHazardTrend(state.hazardEventsList || []);
  }

  function showEventDetail(dateKey) {
    const events = (state.hazardEventsByDate && state.hazardEventsByDate.get(dateKey)) || [];
    text("event-detail-date", new Intl.DateTimeFormat(undefined, { weekday: "long", month: "long", day: "numeric", year: "numeric", timeZone: "UTC" }).format(new Date(`${dateKey}T00:00:00Z`)));
    const list = byId("event-detail-list");
    list.replaceChildren();
    for (const item of events) {
      const card = element("article", "event-detail-item");
      const heading = element("div", "event-detail-item-heading");
      heading.append(element("strong", "", `${label(item.event_type)} · ${item.severity_value} ${item.severity_unit}`));
      const badge = statusBadge(item.is_official ? "verified" : "neutral");
      if (!item.is_official) badge.textContent = "Demonstration";
      heading.append(badge);
      card.append(heading);
      card.append(element("p", "event-detail-time", formatDate(item.occurred_at, true)));
      card.append(element("p", "event-detail-source", item.source_name));
      if (item.notes) card.append(element("p", "event-detail-notes", item.notes));
      if (item.raw_reference) {
        const link = document.createElement("a");
        link.href = item.raw_reference;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.className = "event-detail-link";
        link.textContent = "View source";
        card.append(link);
      }
      list.append(card);
    }
    byId("event-detail-dialog").showModal();
  }

  function renderHazardEventRows(events) {
    const target = byId("hazard-event-rows");
    if (!target) return;
    target.replaceChildren();
    for (const eventRow of events) {
      const row = document.createElement("tr");
      row.append(
        element("td", "", label(eventRow.event_type)),
        element("td", "", formatDate(eventRow.occurred_at, true)),
        element("td", "", `${eventRow.severity_value} ${eventRow.severity_unit}`),
        element("td", "", eventRow.source_name)
      );
      const statusCell = document.createElement("td");
      const badge = statusBadge(eventRow.is_official ? "verified" : "neutral");
      if (!eventRow.is_official) badge.textContent = "Demonstration";
      statusCell.append(badge);
      row.append(statusCell);
      target.append(row);
    }
    if (!events.length) {
      const row = document.createElement("tr");
      const cell = element("td", "", "No hazard events have been logged yet. Use scripts/import_hazard_events.py to add one.");
      cell.colSpan = 5; row.append(cell); target.append(row);
    }
  }

  function renderHazardEvents(payload) {
    const events = payload.events || [];
    text("hazard-events-total", payload.total_logged ?? events.length);
    if (events.length) {
      const occurredDates = events.map((item) => item.occurred_at).sort();
      text("hazard-events-window", `${formatDate(occurredDates[0])} – ${formatDate(occurredDates[occurredDates.length - 1])}`);
    } else {
      text("hazard-events-window", "No events logged yet");
    }
    text("hazard-events-updated", `Updated ${formatDate(payload.generated_at, true)}`);
    state.hazardEventsList = events;
    state.hazardEventsByDate = groupEventsByDate(events);
    if (!state.calendarMonth) {
      const latest = events[0]?.occurred_at;
      state.calendarMonth = startOfMonthUtc(latest ? new Date(latest) : new Date());
    }
    renderHazardEventRows(events);
    setHazardView(state.hazardView || "calendar");
  }

  function reportBadge(status) {
    const badge = statusBadge({ new: "critical", acknowledged: "attention", resolved: "complete" }[status] || status);
    badge.textContent = label(status);
    return badge;
  }

  function isUrgent(item) {
    return item.severity === "life_threatening" || item.damage_type === "injured_or_trapped";
  }

  function renderReports(payload) {
    const items = payload.items || [];
    state.reports = new Map(items.map((item) => [String(item.id), item]));
    const open = items.filter((item) => item.status !== "resolved");
    text("reports-open", open.length);
    text("reports-urgent", open.filter(isUrgent).length);
    text("reports-updated", `Updated ${formatDate(payload.generated_at, true)}`);
    const target = byId("report-rows");
    target.replaceChildren();
    for (const item of items) {
      const row = document.createElement("tr");
      const statusCell = document.createElement("td");
      statusCell.append(reportBadge(item.status));
      const actionCell = document.createElement("td");
      const view = element("button", "button-secondary", "View");
      view.type = "button";
      view.dataset.reportId = item.id;
      view.setAttribute("aria-label", `View report ${item.id}`);
      actionCell.append(view);
      row.append(
        element("td", "", formatDate(item.created_at, true)),
        element("td", "", label(item.damage_type)),
        element("td", isUrgent(item) ? "report-urgent" : "", label(item.severity)),
        element("td", "", item.barangay || "Not identified"),
        element("td", "", item.reporter_name),
        statusCell,
        actionCell
      );
      target.append(row);
    }
    if (!items.length) {
      const row = document.createElement("tr");
      const cell = element("td", "", "No damage reports yet. Reports sent from the public Report Damage page on port 8000 appear here.");
      cell.colSpan = 7; row.append(cell); target.append(row);
    }
  }

  function openReportDialog(id) {
    const item = state.reports.get(String(id));
    if (!item) return;
    text("report-dialog-title", `${label(item.damage_type)} · ${label(item.severity)}`);
    text("report-dialog-received", `Report #${item.id} · ${formatDate(item.created_at, true)}`);
    const phone = document.createElement("a");
    phone.href = `tel:${item.reporter_phone}`;
    phone.textContent = item.reporter_phone;
    const pin = document.createElement("a");
    pin.href = `https://www.openstreetmap.org/?mlat=${item.latitude}&mlon=${item.longitude}#map=18/${item.latitude}/${item.longitude}`;
    pin.target = "_blank";
    pin.rel = "noopener noreferrer";
    pin.textContent = `${Number(item.latitude).toFixed(5)}, ${Number(item.longitude).toFixed(5)} (open map)`;
    const address = [
      item.street,
      item.sitio && `Sitio/Purok ${item.sitio}`,
      item.landmark && `near ${item.landmark}`,
      item.barangay && `Brgy. ${item.barangay}`
    ].filter(Boolean).join(", ") || "No address details given";
    const rows = [
      ["Reporter", item.reporter_name],
      ["Phone", phone],
      ["Location", address],
      ["Pin", pin],
      ["People affected", item.people_affected ?? "Not given"],
      ["Description", item.description || "None"]
    ];
    byId("report-dialog-details").replaceChildren(...rows.flatMap(([term, value]) => {
      const description = document.createElement("dd");
      description.append(typeof value === "object" ? value : String(value));
      return [element("dt", "", term), description];
    }));
    const photos = byId("report-dialog-photos");
    photos.replaceChildren(...item.photos.map((name, index) => {
      const link = document.createElement("a");
      link.href = `/api/v1/admin/reports/${item.id}/photos/${name}`;
      link.target = "_blank";
      link.rel = "noopener";
      const image = document.createElement("img");
      image.src = link.href;
      image.alt = `Report ${item.id} photo ${index + 1}`;
      image.loading = "lazy";
      link.append(image);
      return link;
    }));
    if (!item.photos.length) photos.append(element("p", "event-detail-time", "No photos attached."));
    byId("report-status-select").value = item.status;
    byId("report-status-form").dataset.reportId = item.id;
    text("report-status-message", "");
    byId("report-dialog").showModal();
  }

  async function saveReportStatus(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      await apiFetch(`/api/v1/admin/reports/${form.dataset.reportId}/status`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: byId("report-status-select").value })
      });
      text("report-status-message", "Status saved.");
      await loadDashboard({ quiet: true });
    } catch (error) {
      text("report-status-message", error.message);
    } finally {
      button.disabled = false;
    }
  }

  function renderAnalytics(payload) {
    const summary = payload.summary;
    text("analytics-unique", summary.unique_visitors);
    text("analytics-online", summary.online_now);
    text("analytics-new", summary.new_today);
    text("analytics-views", summary.page_views_today);
    text("analytics-definition", `${payload.definition.unique_visitors} ${payload.definition.online_now} ${payload.definition.privacy}`);
    text("analytics-updated", `Updated ${formatDate(payload.generated_at, true)}`);
    renderChart(payload.series || []);
    const target = byId("top-path-rows");
    target.replaceChildren();
    for (const path of payload.top_paths || []) {
      const row = document.createElement("tr");
      row.append(element("td", "", path.path), element("td", "", path.unique_visitors), element("td", "", path.page_views));
      target.append(row);
    }
    if (!payload.top_paths?.length) {
      const row = document.createElement("tr");
      const cell = element("td", "", "No public page views have been recorded in this isolated environment.");
      cell.colSpan = 3; row.append(cell); target.append(row);
    }
  }

  function openPhotoDialog(centerId) {
    const center = state.centers.get(String(centerId));
    if (!center) return;
    text("photo-center-name", center.name);
    byId("photo-center-id").value = center.id;
    byId("photo-alt").value = center.photo_alt || `Photograph of ${center.name}`;
    byId("photo-source").value = center.photo_source || "";
    byId("photo-source-url").value = center.photo_source_url || "";
    byId("photo-file").value = "";
    text("photo-upload-status", "");
    byId("photo-upload-status").className = "upload-status";
    byId("photo-dialog").showModal();
  }

  async function uploadPhoto(event) {
    event.preventDefault();
    const file = byId("photo-file").files[0];
    const status = byId("photo-upload-status");
    if (!file) { status.textContent = "Choose a photograph first."; status.className = "upload-status error"; return; }
    if (file.size > 5 * 1024 * 1024) { status.textContent = "The photograph must be 5 MB or smaller."; status.className = "upload-status error"; return; }
    const centerId = byId("photo-center-id").value;
    const formData = new FormData();
    formData.append("photo", file);
    formData.append("alt_text", byId("photo-alt").value);
    formData.append("source", byId("photo-source").value);
    formData.append("source_url", byId("photo-source-url").value);
    const submit = byId("submit-photo-upload");
    submit.disabled = true;
    status.textContent = "Uploading and validating photograph…";
    status.className = "upload-status";
    try {
      await apiFetch(`/api/v1/admin/evacuation-centers/${centerId}/photo`, { method: "POST", body: formData, headers: {} });
      status.textContent = "Photograph uploaded to the isolated admin environment.";
      status.className = "upload-status success";
      await loadDashboard({ quiet: true });
      window.setTimeout(() => byId("photo-dialog").close(), 650);
    } catch (error) {
      status.textContent = error.message;
      status.className = "upload-status error";
    } finally {
      submit.disabled = false;
    }
  }

  async function loadDashboard({ quiet = false } = {}) {
    if (state.loading) return;
    state.loading = true;
    byId("refresh-dashboard").disabled = true;
    if (!quiet) { byId("dashboard-status").hidden = false; byId("dashboard-content").hidden = true; }
    byId("dashboard-error").hidden = true;
    try {
      if (page === "analytics") renderAnalytics(await apiFetch("/api/v1/admin/analytics?days=7"));
      else if (page === "hazard-events") renderHazardEvents(await apiFetch("/api/v1/admin/hazard-events"));
      else if (page === "reports") renderReports(await apiFetch("/api/v1/admin/reports"));
      else if (page === "centers") {
        const [barangaysPayload, centersPayload] = await Promise.all([
          apiFetch("/api/v1/admin/barangays"),
          apiFetch("/api/v1/admin/evacuation-centers"),
        ]);
        renderCentersState(centersPayload);
        renderBarangayCards(barangaysPayload);
      } else renderOverview(await apiFetch("/api/v1/admin/overview"));
      byId("dashboard-content").hidden = false;
      byId("dashboard-status").hidden = true;
    } catch (error) {
      if (!quiet) byId("dashboard-content").hidden = true;
      byId("dashboard-status").hidden = true;
      text("dashboard-error-message", error.message || "Unknown dashboard error.");
      byId("dashboard-error").hidden = false;
    } finally {
      state.loading = false;
      byId("refresh-dashboard").disabled = false;
    }
  }

  installSessionControls();
  byId("refresh-dashboard").addEventListener("click", () => loadDashboard({ quiet: true }));
  byId("retry-dashboard").addEventListener("click", () => loadDashboard());
  byId("barangay-center-cards")?.addEventListener("click", handleBarangayCardClick);
  byId("photo-upload-form")?.addEventListener("submit", uploadPhoto);
  byId("close-photo-dialog")?.addEventListener("click", () => byId("photo-dialog").close());
  byId("cancel-photo-upload")?.addEventListener("click", () => byId("photo-dialog").close());
  byId("center-form")?.addEventListener("submit", submitCenterForm);
  byId("close-center-form")?.addEventListener("click", () => byId("center-form-dialog").close());
  byId("cancel-center-form")?.addEventListener("click", () => byId("center-form-dialog").close());
  byId("assign-center-form")?.addEventListener("submit", submitAssignForm);
  byId("close-assign-center")?.addEventListener("click", () => byId("assign-center-dialog").close());
  byId("assign-center-create-instead")?.addEventListener("click", () => {
    const barangayId = byId("assign-center-barangay-id").value;
    const barangayName = state.barangayRows?.get(String(barangayId))?.barangay_name || "this barangay";
    byId("assign-center-dialog").close();
    openCenterForm({ mode: "create", barangayId, barangayName });
  });
  byId("calendar-prev")?.addEventListener("click", () => {
    state.calendarMonth = new Date(Date.UTC(state.calendarMonth.getUTCFullYear(), state.calendarMonth.getUTCMonth() - 1, 1));
    renderCalendar();
  });
  byId("calendar-next")?.addEventListener("click", () => {
    state.calendarMonth = new Date(Date.UTC(state.calendarMonth.getUTCFullYear(), state.calendarMonth.getUTCMonth() + 1, 1));
    renderCalendar();
  });
  byId("close-event-detail")?.addEventListener("click", () => byId("event-detail-dialog").close());
  byId("report-rows")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-report-id]");
    if (button) openReportDialog(button.dataset.reportId);
  });
  byId("report-status-form")?.addEventListener("submit", saveReportStatus);
  byId("close-report-dialog")?.addEventListener("click", () => byId("report-dialog").close());
  document.querySelectorAll("[data-hazard-view]").forEach((button) => {
    button.addEventListener("click", () => setHazardView(button.dataset.hazardView));
  });
  window.setInterval(() => { if (document.visibilityState === "visible") loadDashboard({ quiet: true }); }, 60_000);
  loadDashboard();
})();
