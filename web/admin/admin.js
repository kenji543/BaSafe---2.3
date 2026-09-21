(() => {
  "use strict";

  const page = document.body.dataset.adminPage || "overview";
  const byId = (id) => document.getElementById(id);
  const state = { loading: false, centers: new Map() };

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

  function definitionList(target, items) {
    if (!target) return;
    target.replaceChildren();
    for (const [term, description] of items) target.append(element("dt", "", term), element("dd", "", description));
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

  function renderRouting(routing) {
    if (!byId("routing-status")) return;
    const available = Boolean(routing.routing_available);
    const badge = byId("routing-status");
    badge.className = `status-badge ${available ? "available" : "unavailable"}`;
    badge.textContent = available ? "Available" : "Unavailable";
    const studyArea = routing.study_area || {};
    const roadGraph = routing.road_graph || {};
    definitionList(byId("routing-details"), [
      ["Study area", studyArea.name || "Not loaded"],
      ["Boundary status", studyArea.is_official ? "Official-source" : "Research-defined"],
      ["Road data", roadGraph.date ? `OpenStreetMap · ${formatDate(roadGraph.date)}` : "Not loaded"],
      ["Centers loaded", String(routing.center_count ?? 0)]
    ]);
    const dependencies = byId("routing-dependencies");
    dependencies.replaceChildren();
    for (const [name, value] of Object.entries(routing.dependencies || {})) dependencies.append(element("span", `dependency ${value ? "" : "missing"}`, `${label(name)}: ${value ? "ready" : "missing"}`));
  }

  function placeholderPhoto() {
    const container = element("div", "center-photo");
    const icon = svgElement("svg", { viewBox: "0 0 48 48", "aria-hidden": "true" });
    icon.append(svgElement("path", { d: "M7 14h9l3-4h10l3 4h9v25H7V14Z" }), svgElement("circle", { cx: "24", cy: "26", r: "8" }));
    container.append(icon, element("span", "", "No verified photograph"));
    return container;
  }

  function renderCenterCards(centers) {
    const target = byId("evacuation-center-cards");
    if (!target) return;
    state.centers.clear();
    target.replaceChildren();
    for (const center of centers.items) {
      state.centers.set(String(center.id), center);
      const card = element("article", "center-admin-card");
      let media = placeholderPhoto();
      if (center.photo_url) {
        media = element("div", "center-photo");
        const image = document.createElement("img");
        image.src = center.photo_url;
        image.alt = center.photo_alt || `Photograph of ${center.name}`;
        image.loading = "lazy";
        media.append(image);
      }
      const copy = element("div", "center-card-copy");
      copy.append(element("h2", "", center.name), element("span", "", center.barangay || "Barangay not reported"));
      const meta = element("div", "center-card-meta");
      meta.append(element("span", "", center.is_official ? "Official-source record" : "Reference record"), element("span", "", center.capacity === null ? "Capacity not recorded" : `Capacity ${center.capacity}`));
      copy.append(meta, element("div", "center-photo-source", center.has_photo ? `Photo: ${center.photo_source || "Source not reported"}` : "A verified facility photo has not been supplied."));
      const button = element("button", "upload-photo-button", center.has_photo ? "Replace photograph" : "Upload photograph");
      button.type = "button";
      button.dataset.centerId = center.id;
      copy.append(button);
      card.append(media, copy);
      target.append(card);
    }
  }

  function renderCenterCoverage(centers) {
    const active = Math.max(centers.active, 0);
    const photoPercent = active ? Math.round((centers.with_photos / active) * 100) : 0;
    const capacityPercent = active ? Math.round((centers.with_capacity / active) * 100) : 0;
    text("center-count-badge", `${active} active centers`);
    text("photo-coverage-label", `${centers.with_photos}/${active}`);
    text("capacity-coverage-label", `${centers.with_capacity}/${active}`);
    if (byId("photo-coverage-bar")) byId("photo-coverage-bar").style.width = `${photoPercent}%`;
    if (byId("capacity-coverage-bar")) byId("capacity-coverage-bar").style.width = `${capacityPercent}%`;
    renderCenterCards(centers);
  }

  function renderContext(context) {
    text("context-incidents", context.historical_incidents);
    text("context-incidents-official", `${context.official_historical_incidents} official-source`);
    text("context-clup", context.clup_references);
    text("context-clup-official", `${context.official_clup_references} official-source`);
    text("context-barangays", context.barangays);
    text("context-barangays-official", `${context.official_barangays} official-source`);
    text("context-boundary", context.municipal_boundaries);
    text("context-boundary-official", `${context.official_municipal_boundaries} official-source`);
  }

  function renderActivity(scoring) {
    const target = byId("activity-rows");
    if (!target) return;
    text("activity-count", `${scoring.total} saved`);
    target.replaceChildren();
    const items = scoring.recent || [];
    byId("activity-empty").hidden = items.length > 0;
    byId("activity-table").hidden = items.length === 0;
    for (const score of items) {
      const row = document.createElement("tr");
      const coordinateLabel = Number.isFinite(Number(score.latitude)) && Number.isFinite(Number(score.longitude)) ? `${Number(score.latitude).toFixed(5)}, ${Number(score.longitude).toFixed(5)}` : "Location not reported";
      row.append(element("td", "", score.location_label || coordinateLabel), element("td", "", score.barangay || "Not matched"), element("td", "", score.score === null || score.score === undefined ? "Incomplete" : `${Math.round(score.score)} · ${score.category || "Category not reported"}`));
      const statusCell = document.createElement("td");
      statusCell.append(statusBadge(score.status));
      row.append(statusCell, element("td", "", formatDate(score.created_at, true)));
      target.append(row);
    }
  }

  function renderOverview(payload) {
    renderSummary(payload);
    renderAlerts(payload.alerts || []);
    renderDatasets(payload.datasets || []);
    renderRouting(payload.routing || {});
    renderCenterCoverage(payload.evacuation_centers || { active: 0, items: [], with_photos: 0, with_capacity: 0 });
    renderContext(payload.context || {});
    renderActivity(payload.scoring || { total: 0, recent: [] });
  }

  function renderChart(series) {
    const svg = byId("visitor-chart");
    if (!svg) return;
    svg.replaceChildren();
    const width = 760, height = 280, left = 46, right = 20, top = 20, bottom = 45;
    const plotWidth = width - left - right, plotHeight = height - top - bottom;
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
      else renderOverview(await apiFetch("/api/v1/admin/overview"));
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
  byId("evacuation-center-cards")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-center-id]");
    if (button) openPhotoDialog(button.dataset.centerId);
  });
  byId("photo-upload-form")?.addEventListener("submit", uploadPhoto);
  byId("close-photo-dialog")?.addEventListener("click", () => byId("photo-dialog").close());
  byId("cancel-photo-upload")?.addEventListener("click", () => byId("photo-dialog").close());
  window.setInterval(() => { if (document.visibilityState === "visible") loadDashboard({ quiet: true }); }, 60_000);
  loadDashboard();
})();
