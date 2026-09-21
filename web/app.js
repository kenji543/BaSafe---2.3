(() => {
  "use strict";

  const API_BASE = (document.body.dataset.apiBase || "/api/v1").replace(/\/$/, "");
  const BASEY_CENTER = [11.282, 125.069];
  const BASEY_FALLBACK_BOUNDS = [[11.2540, 124.9764], [11.5641, 125.3092]];
  const BASEMAP_DEFINITIONS = {
    streets: {
      label: "Streets",
      shortAttribution: "Basemap: OpenStreetMap",
      url: "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
      options: {
        maxZoom: 19,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
        crossOrigin: true
      }
    },
    satellite: {
      label: "Satellite",
      shortAttribution: "Basemap: Esri World Imagery",
      url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
      options: {
        maxZoom: 19,
        maxNativeZoom: 18,
        attribution: "Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community",
        crossOrigin: true
      }
    },
    terrain: {
      label: "Terrain",
      shortAttribution: "Basemap: Esri World Topographic Map",
      url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
      options: {
        maxZoom: 19,
        attribution: "Tiles &copy; Esri &mdash; Esri and its contributors",
        crossOrigin: true
      }
    }
  };
  const HISTORY_KEY = "geosafe-fis.recent-assessments.v1";
  const PRIVATE_ASSESSMENT_ID = /^[A-Za-z0-9_-]{20,128}$/;
  const REQUIRED_HAZARDS = [
    { key: "flood", label: "Flood", color: "#2876a8" },
    { key: "liquefaction", label: "Liquefaction", color: "#d9903d" },
    { key: "ground_shaking", label: "Ground shaking", color: "#b74b53" }
  ];
  const MAP_HAZARDS = [
    ...REQUIRED_HAZARDS,
    {
      key: "rain_induced_landslide",
      label: "Rain-induced landslide",
      color: "#8f3d24",
      viewOnly: true
    }
  ];
  const DISCLAIMER = "This report is a preliminary multi-hazard screening output based on selected available data. It does not certify that a location is safe or unsafe and does not replace official hazard, planning, engineering, geological, geotechnical, or regulatory assessment.";
  const AVAILABLE_HAZARD_STATUS = new Set(["available", "verified", "success", "ok"]);
  const HAZARD_STATUS_MESSAGES = {
    available: "Official classification available",
    no_intersection: "No intersecting polygon at this location",
    outside_coverage: "Location is outside the mapped dataset coverage",
    unavailable: "Verified source is unavailable",
    authentication_required: "Authorized ArcGIS access is required",
    service_error: "The source service returned an error",
    timeout: "The source service timed out",
    invalid_response: "The source response could not be validated",
    changed_schema: "Live source fields differ from the verified schema",
    incomplete: "Required evidence is incomplete",
    pending_verification: "Source endpoint is pending verification",
    unknown_code: "The returned source code is not in the verified domain"
  };

  const state = {
    map: null,
    tileLayer: null,
    tileLoaded: false,
    baseLayers: new Map(),
    basemapReady: new Set(),
    activeBasemap: "satellite",
    boundaryGeoJson: null,
    barangayGeoJson: null,
    boundaryStatus: "Status not reported",
    barangayStatus: "Status not reported",
    boundaryLayer: null,
    barangayLayer: null,
    hazardDatasets: [],
    hazardLayers: new Map(),
    ulapStatus: null,
    ulapStatusLoaded: false,
    ulapStatusFailed: false,
    ulapServices: [],
    runtimeDataMode: "snapshot",
    liveHazardResponse: null,
    liveHazards: new Map(),
    liveHazardCoordinates: null,
    liveRequestSequence: 0,
    marker: null,
    selection: null,
    assessment: null,
    assessmentRunning: false,
    routingStatus: null,
    routeRequestRunning: false,
    routeLayers: new Map(),
    routeDestinationMarker: null,
    searchHighlightLayer: null,
    searchRequestSequence: 0,
    mapNotices: new Set(),
    recent: []
  };

  const els = {};
  let controlsReturnFocus = null;

  function cacheElements() {
    [
      "api-status", "search-form", "location-search", "search-results",
      "coordinate-form", "latitude", "longitude", "inside-badge",
      "selection-summary", "ulap-point-status", "run-assessment", "toggle-boundary",
      "toggle-barangays", "basemap-controls", "basemap-attribution",
      "hazard-layer-controls", "map-legend",
      "ulap-health-badge", "ulap-health-summary", "ulap-service-list",
      "history-list", "map", "map-loading", "map-notice",
      "fit-basey", "clear-selection", "map-data-state",
      "results-empty", "results-loading", "results-content",
      "results-empty-title", "results-empty-copy", "results-empty-steps", "empty-primary-action",
      "assessment-status", "result-actions", "preview-report",
      "download-report", "report-dialog", "close-report",
      "report-preview", "print-preview", "download-report-dialog",
      "toast-region", "use-location", "reset-map", "open-controls",
      "close-controls", "controls-backdrop", "mobile-assess", "start-new-assessment",
      "mobile-view-switcher", "mobile-view-map", "mobile-view-score",
      "mobile-selection-summary", "mobile-selection-name", "mobile-selection-badge",
      "mobile-hazard-summary", "mobile-selection-details",
      "routing-panel", "routing-badge", "routing-status", "routing-controls",
      "find-evacuation-route", "map-evacuation-route", "routing-result", "route-layer-controls",
      "toggle-evacuation-route", "clear-route", "map-route-summary",
      "map-route-destination", "map-route-distance", "map-route-clear"
    ].forEach((id) => {
      els[id] = document.getElementById(id);
    });
  }

  function firstDefined(...values) {
    return values.find((value) => value !== undefined && value !== null);
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function textValue(value, fallback = "Not reported") {
    if (value === undefined || value === null || value === "") return fallback;
    return String(value);
  }

  function titleCase(value) {
    return textValue(value, "Not reported")
      .replaceAll("_", " ")
      .replaceAll("-", " ")
      .replace(/\b\w/g, (letter) => letter.toUpperCase());
  }

  function toNumber(value) {
    if (value === "" || value === null || value === undefined) return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function clamp(value, min = 0, max = 1) {
    return Math.min(max, Math.max(min, value));
  }

  function formatCoordinate(value) {
    const number = toNumber(value);
    return number === null ? "Not available" : number.toFixed(6);
  }

  function formatValue(value, digits = 3) {
    const number = toNumber(value);
    return number === null ? "Not available" : number.toFixed(digits).replace(/\.?0+$/, "");
  }

  function formatDate(value) {
    if (!value) return "Date not reported";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric"
    }).format(date);
  }

  function formatDateTime(value) {
    if (!value) return "Time not reported";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
      timeZoneName: "short"
    }).format(date);
  }

  function normalizeStatus(value, fallback = "unavailable") {
    const status = textValue(value, fallback).trim().toLowerCase().replace(/[^a-z0-9]+/g, "_");
    if (status === "healthy" || status === "ready" || status === "operational" || status === "verified" || status === "official" || status === "authoritative") return "available";
    if (status === "verified_with_changed_metadata" || status === "metadata_changed") return "changed_schema";
    if (status === "invalid_layer" || status === "missing_layer") return "invalid_response";
    if (status === "inaccessible") return "service_error";
    if (status === "no_feature" || status === "zero_features" || status === "not_found_at_point") return "no_intersection";
    if (status === "outside_dataset" || status === "not_covered") return "outside_coverage";
    if (status === "auth_required" || status === "unauthorized" || status === "forbidden") return "authentication_required";
    if (status === "error" || status === "failed" || status === "offline") return "service_error";
    if (status === "schema_changed" || status === "missing_classification_field") return "changed_schema";
    if (status === "not_configured" || status === "missing_endpoint") return "unavailable";
    return status || fallback;
  }

  function statusKind(status) {
    const normalized = normalizeStatus(status);
    if (AVAILABLE_HAZARD_STATUS.has(normalized)) return "success";
    if (normalized === "pending_verification" || normalized === "authentication_required" || normalized === "changed_schema") return "warning";
    if (normalized === "no_intersection" || normalized === "outside_coverage" || normalized === "unavailable") return "neutral";
    return "danger";
  }

  function statusLabel(status) {
    return titleCase(normalizeStatus(status));
  }

  function statusMessage(status, fallback) {
    const normalized = normalizeStatus(status);
    return HAZARD_STATUS_MESSAGES[normalized] || fallback || statusLabel(status);
  }

  function hazardClassificationPresentation(hazard) {
    const label = textValue(hazard?.officialLabel, `Code ${textValue(hazard?.officialCode)}`);
    const mappedNone = /^(none|no susceptibility|not susceptible|no mapped susceptibility)$/i.test(label.trim());
    return {
      label,
      pointLabel: mappedNone ? `${label} (mapped class)` : label,
      note: mappedNone
        ? `The source explicitly reports “${label}” at this point. This is mapped source evidence—not missing data and not a declaration that the site is safe.`
        : null
    };
  }

  function safeSourceUrl(value) {
    if (!value) return null;
    try {
      const url = new URL(String(value), window.location.origin);
      if (!["http:", "https:"].includes(url.protocol)) return null;
      ["token", "access_token", "key", "api_key"].forEach((name) => url.searchParams.delete(name));
      return url.href;
    } catch {
      return null;
    }
  }

  function cacheMetadata(value) {
    const cache = value && typeof value === "object" ? value : {};
    const fromCache = normalizeBoolean(firstDefined(cache.fromCache, cache.from_cache, cache.hit));
    const source = textValue(firstDefined(
      cache.source,
      cache.mode,
      fromCache === true ? "cache" : fromCache === false ? "live" : null
    ), "not reported");
    const retrievedAt = firstDefined(cache.retrievedAt, cache.retrieved_at, cache.cachedAt, cache.cached_at);
    const expiresAt = firstDefined(cache.expiresAt, cache.expires_at);
    const expires = expiresAt ? new Date(expiresAt) : null;
    const stale = normalizeBoolean(firstDefined(cache.stale, cache.is_stale)) === true
      || Boolean(expires && !Number.isNaN(expires.getTime()) && expires.getTime() < Date.now());
    return { source, retrievedAt, expiresAt, stale };
  }

  function unwrap(payload) {
    if (!payload || typeof payload !== "object") return payload;
    if ("data" in payload && payload.data !== undefined) return payload.data;
    return payload;
  }

  function asArray(payload) {
    const value = unwrap(payload);
    if (Array.isArray(value)) return value;
    if (!value || typeof value !== "object") return [];
    for (const key of [
      "items", "results", "records", "assessments", "datasets", "hazard_layers",
      "sources", "data_sources", "rules", "features", "locations", "incidents",
      "historical_incidents", "clup_references", "services", "layers"
    ]) {
      if (Array.isArray(value[key])) return value[key];
    }
    return [];
  }

  function setApiStatus(kind, message) {
    if (!els["api-status"]) return;
    els["api-status"].className = `system-status is-${kind}`;
    const label = els["api-status"].querySelector("span:last-child");
    if (label) label.textContent = message;
  }

  async function apiFetch(path, options = {}) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), options.timeout || 15000);
    const url = path.startsWith("http") ? path : `${API_BASE}${path}`;
    const headers = new Headers(options.headers || {});
    headers.set("Accept", "application/json");
    if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");

    try {
      const response = await fetch(url, { ...options, headers, signal: controller.signal });
      setApiStatus("online", "Data service available");
      if (!response.ok) {
        let payload = null;
        try {
          payload = await response.json();
        } catch {
          payload = null;
        }
        const error = new Error(payload?.error?.message || `Request failed (${response.status})`);
        error.status = response.status;
        error.code = payload?.error?.code;
        error.details = payload?.error?.details;
        throw error;
      }
      if (response.status === 204) return null;
      const contentType = response.headers.get("content-type") || "";
      return contentType.includes("json") ? response.json() : response.text();
    } catch (error) {
      if (!error.status) setApiStatus("offline", "Data service unavailable");
      throw error;
    } finally {
      window.clearTimeout(timeout);
    }
  }

  async function optionalFetch(path) {
    try {
      return await apiFetch(path);
    } catch {
      return null;
    }
  }

  function showToast(message, type = "") {
    const toast = document.createElement("div");
    toast.className = `toast ${type}`.trim();
    toast.textContent = message;
    els["toast-region"].appendChild(toast);
    window.setTimeout(() => toast.remove(), 5200);
  }

  function setMapNotice(key, message) {
    if (message) state.mapNotices.add(`${key}::${message}`);
    else {
      for (const notice of state.mapNotices) {
        if (notice.startsWith(`${key}::`)) state.mapNotices.delete(notice);
      }
    }
    const messages = [...state.mapNotices].map((entry) => entry.split("::").slice(1).join("::"));
    els["map-notice"].hidden = messages.length === 0;
    els["map-notice"].textContent = messages.join(" ");
  }

  function initMap() {
    if (!window.L) {
      els.map.classList.add("is-unavailable");
      els.map.innerHTML = `<div class="empty-api-block"><strong>Interactive map library unavailable.</strong><br>You can still enter coordinates above when the scoring service is available.</div>`;
      els["map-loading"].hidden = true;
      setMapNotice("leaflet", "The interactive map could not load. Coordinate entry remains available.");
      return;
    }

    state.map = L.map("map", {
      zoomControl: true,
      attributionControl: true,
      minZoom: 9,
      maxZoom: 19,
      maxBounds: BASEY_FALLBACK_BOUNDS,
      maxBoundsViscosity: 1
    }).setView(BASEY_CENTER, 11);

    [
      ["municipalBoundaryPane", 410],
      ["barangayBoundaryPane", 420],
      ["hazardOverlayPane", 430],
      ["searchResultPane", 435],
      ["routePane", 470]
    ].forEach(([name, zIndex]) => {
      state.map.createPane(name);
      state.map.getPane(name).style.zIndex = String(zIndex);
    });

    Object.entries(BASEMAP_DEFINITIONS).forEach(([key, definition]) => {
      const layer = L.tileLayer(definition.url, definition.options);
      layer.on("tileload", () => {
        state.basemapReady.add(key);
        if (state.activeBasemap === key) {
          state.tileLoaded = true;
          setMapNotice("basemap", null);
        }
      });
      layer.on("tileerror", () => {
        if (state.activeBasemap === key && !state.basemapReady.has(key)) {
          setMapNotice("basemap", `${definition.label} basemap tiles are unavailable; loaded overlays and coordinate selection can still be used.`);
        }
      });
      state.baseLayers.set(key, layer);
    });
    setBasemap("satellite");
    state.map.on("click", selectLocationFromMapEvent);
  }

  function lockMapToBasey(bounds) {
    if (!state.map || !bounds?.isValid?.()) return;
    const navigationBounds = bounds.pad(.04);
    state.map.setMaxBounds(navigationBounds);
    const fittedZoom = state.map.getBoundsZoom(navigationBounds, false);
    state.map.setMinZoom(Math.max(9, fittedZoom));
  }

  function setBasemap(key) {
    const definition = BASEMAP_DEFINITIONS[key];
    const nextLayer = state.baseLayers.get(key);
    if (!state.map || !definition || !nextLayer) return;
    if (state.tileLayer && state.tileLayer !== nextLayer) {
      state.map.removeLayer(state.tileLayer);
    }
    state.activeBasemap = key;
    state.tileLayer = nextLayer;
    state.tileLoaded = state.basemapReady.has(key);
    if (!state.map.hasLayer(nextLayer)) nextLayer.addTo(state.map);
    nextLayer.bringToBack();
    if (els["basemap-attribution"]) {
      els["basemap-attribution"].textContent = definition.shortAttribution;
    }
    setMapNotice("basemap", null);
    window.setTimeout(() => {
      if (state.activeBasemap === key && !state.basemapReady.has(key)) {
        setMapNotice("basemap", `${definition.label} basemap tiles are unavailable; loaded overlays and coordinate selection can still be used.`);
      }
    }, 7000);
  }

  function parseGeoJson(value) {
    let candidate = unwrap(value);
    if (!candidate) return null;
    if (typeof candidate === "string") {
      try {
        candidate = JSON.parse(candidate);
      } catch {
        return null;
      }
    }
    for (const key of ["geojson", "geometry_data", "boundary", "feature_collection"]) {
      if (candidate && candidate[key]) return parseGeoJson(candidate[key]);
    }
    if (candidate.type === "FeatureCollection" && Array.isArray(candidate.features)) return candidate;
    if (candidate.type === "Feature") return { type: "FeatureCollection", features: [candidate] };
    if (candidate.type && candidate.coordinates) {
      return {
        type: "FeatureCollection",
        features: [{ type: "Feature", properties: {}, geometry: candidate }]
      };
    }
    if (Array.isArray(candidate)) {
      const features = candidate
        .map((item) => {
          if (item?.type === "Feature") return item;
          if (item?.geometry) {
            return {
              type: "Feature",
              id: item.id,
              properties: item.properties || item,
              geometry: item.geometry
            };
          }
          return null;
        })
        .filter(Boolean);
      return features.length ? { type: "FeatureCollection", features } : null;
    }
    if (Array.isArray(candidate.features)) {
      return { type: "FeatureCollection", features: candidate.features };
    }
    return null;
  }

  function featureLabel(feature, fallback = "Unnamed area") {
    const properties = feature?.properties || {};
    return textValue(firstDefined(
      properties.name,
      properties.barangay_name,
      properties.brgy_name,
      properties.label,
      properties.NAME_3,
      properties.BRGY
    ), fallback);
  }

  async function loadBoundary() {
    const payload = await apiFetch("/boundary");
    const raw = payload && typeof payload === "object" ? payload : {};
    const parsedGeoJson = parseGeoJson(payload);
    const geoJson = parsedGeoJson ? {
      ...parsedGeoJson,
      features: parsedGeoJson.features.filter((feature) =>
        feature.properties?.is_demo !== true
        && !/demo|synthetic|placeholder|mock/i.test(textValue(firstDefined(feature.properties?.data_status, feature.properties?.source_status), ""))
      )
    } : null;
    if (!geoJson?.features?.length) throw new Error("No verified live municipal boundary features returned");
    const featureStatuses = geoJson.features.map((feature) => dataStatus(feature.properties || {}));
    state.boundaryStatus = featureStatuses.some((status) => /demo|sample|placeholder/i.test(status))
      ? "Demonstration"
      : featureStatuses.length && featureStatuses.every((status) => /official|authoritative|verified|available/i.test(status))
        ? (featureStatuses.every((status) => /official|authoritative/i.test(status)) ? "Official" : "Verified")
        : dataStatus(firstDefined(raw.metadata, raw.source_metadata, raw.data?.metadata, raw));
    const notices = normalizeStringList(raw.notices);
    if (notices.length) setMapNotice("boundary-data", notices.join(" "));
    state.boundaryGeoJson = geoJson;
    if (state.map) {
      state.boundaryLayer = L.geoJSON(geoJson, {
        pane: "municipalBoundaryPane",
        style: {
          color: "#123c36",
          weight: 3,
          opacity: .95,
          fillColor: "#5fa890",
          fillOpacity: .055,
          dashArray: null
        },
        onEachFeature: (feature, layer) => {
          const status = dataStatus(feature.properties || {});
          layer.bindPopup(`<strong>${escapeHtml(featureLabel(feature, "Basey municipal boundary"))}</strong><br><small>${escapeHtml(status)} municipal boundary</small>`);
          layer.on("click", selectLocationFromMapEvent);
        }
      }).addTo(state.map);
      const bounds = state.boundaryLayer.getBounds();
      if (bounds.isValid()) {
        lockMapToBasey(bounds);
        state.map.fitBounds(bounds.pad(.04));
      }
    }
  }

  async function loadBarangays() {
    const payload = await apiFetch("/barangays");
    const raw = payload && typeof payload === "object" ? payload : {};
    const parsedGeoJson = parseGeoJson(payload);
    const geoJson = parsedGeoJson ? {
      ...parsedGeoJson,
      features: parsedGeoJson.features.filter((feature) =>
        feature.properties?.is_demo !== true
        && !/demo|synthetic|placeholder|mock/i.test(textValue(firstDefined(feature.properties?.data_status, feature.properties?.source_status), ""))
      )
    } : null;
    if (!geoJson?.features?.length) throw new Error("No verified live barangay features returned");
    const featureStatuses = geoJson.features.map((feature) => dataStatus(feature.properties || {}));
    state.barangayStatus = featureStatuses.some((status) => /demo|sample|placeholder/i.test(status))
      ? "Demonstration"
      : featureStatuses.length && featureStatuses.every((status) => /official|authoritative|verified|available/i.test(status))
        ? (featureStatuses.every((status) => /official|authoritative/i.test(status)) ? "Official" : "Verified")
        : dataStatus(firstDefined(raw.metadata, raw.source_metadata, raw.data?.metadata, raw));
    const notices = normalizeStringList(raw.notices);
    if (notices.length) setMapNotice("barangay-data", notices.join(" "));
    state.barangayGeoJson = geoJson;
    if (state.map) {
      state.barangayLayer = L.geoJSON(geoJson, {
        pane: "barangayBoundaryPane",
        style: {
          color: "#3d7b6b",
          weight: 1,
          opacity: .85,
          fillColor: "#74b7a2",
          fillOpacity: .04,
          dashArray: "4 3"
        },
        onEachFeature: (feature, layer) => {
          const name = featureLabel(feature);
          const status = dataStatus(feature.properties || {});
          layer.bindTooltip(escapeHtml(name), { sticky: true });
          layer.bindPopup(`<strong>${escapeHtml(name)}</strong><br><small>${escapeHtml(status)} boundary · click the map to select this point.</small>`);
          layer.on("click", selectLocationFromMapEvent);
        }
      }).addTo(state.map);
    }
  }

  function datasetType(dataset) {
    const value = [
      dataset.key,
      dataset.hazard_type,
      dataset.hazard,
      dataset.hazardName,
      dataset.hazard_name,
      dataset.type,
      dataset.code,
      dataset.slug,
      dataset.name,
      dataset.service,
      dataset.layerName,
      dataset.layer_name
    ].filter((item) => item !== undefined && item !== null).join(" ").toLowerCase();
    if (value.includes("landslide") || value.includes("rain_induced")) return "rain_induced_landslide";
    if (value.includes("liqu")) return "liquefaction";
    if (value.includes("shak") || value.includes("ground_motion")) return "ground_shaking";
    if (value.includes("flood")) return "flood";
    return value.replace(/[^a-z0-9]+/g, "_") || "hazard";
  }

  function dataStatus(dataset) {
    const explicit = firstDefined(dataset.is_demo, dataset.demonstration, dataset.is_official);
    if (explicit === true && dataset.is_official !== true) return "Demonstration";
    if (dataset.is_official === true) return "Official";
    const status = textValue(firstDefined(dataset.data_status, dataset.status, dataset.provenance_status), "");
    if (/demo|sample|placeholder/i.test(status)) return "Demonstration";
    if (/official|authoritative/i.test(status)) return "Official";
    return status || "Status not reported";
  }

  function serviceKey(service) {
    return textValue(firstDefined(
      service.key,
      service.service,
      service.hazard_type,
      service.hazard,
      service.slug
    ), "").trim().toLowerCase().replace(/[^a-z0-9]+/g, "_");
  }

  function hazardLayerStateKey(dataset) {
    return serviceKey(dataset)
      || datasetType(dataset)
      || String(firstDefined(dataset.id, dataset.layerId, dataset.layer_id, dataset.name, "hazard"));
  }

  function serviceStatus(service) {
    if (!state.ulapStatusLoaded) return "pending_verification";
    if (state.ulapStatusFailed) return "service_error";
    const configuredFallback = normalizeBoolean(service.configured) === false
      ? "unavailable"
      : "pending_verification";
    return normalizeStatus(firstDefined(
      service.runtime_validation?.status,
      service.runtimeValidation?.status,
      service.verification?.status,
      service.verificationStatus,
      service.verification_status,
      service.healthStatus,
      service.health_status,
      service.availabilityStatus,
      service.availability_status,
      service.status
    ), configuredFallback);
  }

  function serviceName(service, fallback = "ULAP service") {
    return textValue(firstDefined(
      service.displayName,
      service.display_name,
      service.dataset,
      service.layerName,
      service.layer_name,
      service.name,
      service.service
    ), fallback);
  }

  function serviceLayerUrl(service) {
    return safeSourceUrl(firstDefined(
      service.layerUrl,
      service.layer_url,
      service.sourceUrl,
      service.source_url,
      service.url
    ));
  }

  function serviceDisplayUrl(service) {
    const key = serviceKey(service);
    const isRequiredHazard = REQUIRED_HAZARDS.some((definition) => definition.key === key);
    const isMapOverlay = MAP_HAZARDS.some((definition) => definition.key === key && definition.viewOnly);
    const datasetKind = textValue(service.dataset_type, "hazard").trim().toLowerCase();
    const isHazardDataset = datasetKind === "hazard" || datasetKind === "hazard_overlay";
    if (normalizeBoolean(service.configured) !== true || !isHazardDataset) return null;
    if (isMapOverlay) return serviceLayerUrl(service);
    if (!isRequiredHazard) return null;
    return `/hazard-layers/${encodeURIComponent(key)}/features`;
  }

  function serviceAttribution(service) {
    return textValue(firstDefined(
      service.attribution,
      service.copyrightText,
      service.copyright_text,
      service.agency,
      service.provider,
      service.sourceName,
      service.source_name,
      service.metadata?.source_name
    ), "Attribution not reported");
  }

  function normalizeServiceCollection(payload) {
    const value = unwrap(payload);
    const services = asArray(value);
    if (services.length) return services;
    const root = firstDefined(value?.services, value?.serviceRegistry, value?.service_registry);
    if (root && typeof root === "object") {
      const flattened = [];
      const visit = (node, path = []) => {
        if (Array.isArray(node)) {
          node.forEach((item, index) => visit(item, [...path, String(index)]));
          return;
        }
        if (!node || typeof node !== "object") return;
        const looksLikeService = [
          "layerUrl", "layer_url", "sourceUrl", "source_url", "serviceUrl", "service_url",
          "layerId", "layer_id", "classificationField", "classification_field",
          "verificationStatus", "verification_status"
        ].some((key) => node[key] !== undefined);
        if (looksLikeService) {
          flattened.push({ hazard: firstDefined(node.hazard, node.hazard_type, path.at(-1)), ...node });
          return;
        }
        Object.entries(node).forEach(([key, child]) => {
          if (child && typeof child === "object") visit(child, [...path, key]);
          else if (/status/i.test(key) && path.length) flattened.push({ hazard: path.at(-1), status: child });
        });
      };
      visit(root);
      return flattened;
    }
    return [];
  }

  function mergeUlapStatusIntoServices() {
    const validations = asArray(firstDefined(
      state.ulapStatus?.services,
      state.ulapStatus?.service_results,
      state.ulapStatus?.results
    ));
    if (!validations.length || !state.ulapServices.length) return;
    const byKey = new Map(validations.map((validation) => [
      serviceKey(validation),
      validation
    ]).filter(([key]) => key));
    state.ulapServices = state.ulapServices.map((service) => {
      const validation = byKey.get(serviceKey(service));
      if (!validation) return service;
      return {
        ...service,
        runtime_validation: {
          ...(service.runtime_validation || {}),
          ...validation
        },
        live_status_received: true
      };
    });
    state.hazardDatasets = state.ulapServices.filter((service) =>
      REQUIRED_HAZARDS.some((definition) => datasetType(service) === definition.key)
    );
  }

  async function loadUlapStatus() {
    try {
      const payload = unwrap(await apiFetch("/ulap/status")) || {};
      state.ulapStatus = payload;
      state.ulapStatusLoaded = true;
      state.ulapStatusFailed = false;
      mergeUlapStatusIntoServices();
      renderUlapHealth();
      renderHazardControls();
      return payload;
    } catch (error) {
      state.ulapStatus = {
        status: "service_error",
        message: "Live ULAP validation status could not be retrieved."
      };
      state.ulapStatusLoaded = true;
      state.ulapStatusFailed = true;
      renderUlapHealth();
      renderHazardControls();
      throw error;
    }
  }

  async function loadUlapServices() {
    const payload = await apiFetch("/ulap/services");
    state.ulapServices = normalizeServiceCollection(payload);
    mergeUlapStatusIntoServices();
    state.hazardDatasets = state.ulapServices.filter((service) =>
      REQUIRED_HAZARDS.some((definition) => datasetType(service) === definition.key)
    );
    renderUlapHealth();
    renderHazardControls();
    return payload;
  }

  async function loadRuntimeHazardRegistry() {
    const health = unwrap(await apiFetch("/health")) || {};
    state.runtimeDataMode = textValue(firstDefined(
      health.runtimeDataMode,
      health.runtime_data_mode
    ), "snapshot").trim().toLowerCase();
    if (state.runtimeDataMode === "live") {
      await loadUlapStatus();
      return loadUlapServices();
    }

    const payload = unwrap(await apiFetch("/hazard-layers")) || {};
    state.ulapServices = normalizeServiceCollection(payload);
    state.hazardDatasets = state.ulapServices.filter((service) =>
      REQUIRED_HAZARDS.some((definition) => datasetType(service) === definition.key)
    );
    const missing = REQUIRED_HAZARDS.filter((definition) =>
      !state.hazardDatasets.some((dataset) => datasetType(dataset) === definition.key)
    );
    state.ulapStatus = {
      status: missing.length ? "degraded" : "available",
      notice: missing.length
        ? `Missing local hazard data: ${missing.map((item) => item.label).join(", ")}.`
        : "All required local hazard data is active.",
      runtimeDataMode: "snapshot"
    };
    state.ulapStatusLoaded = true;
    state.ulapStatusFailed = false;
    renderUlapHealth();
    renderHazardControls();
    return payload;
  }

  function renderUlapHealth() {
    const snapshotMode = state.runtimeDataMode === "snapshot";
    const rootStatus = normalizeStatus(firstDefined(
      state.ulapStatus?.overallStatus,
      state.ulapStatus?.overall_status,
      state.ulapStatus?.status,
      state.ulapStatus?.health
    ), state.ulapStatusLoaded
      ? (state.ulapServices.length ? "available" : "pending_verification")
      : "pending_verification");
    const kind = statusKind(rootStatus);
    els["ulap-health-badge"].className = `badge ${kind}`;
    els["ulap-health-badge"].textContent = statusLabel(rootStatus);
    const checkedAt = firstDefined(
      state.ulapStatus?.checkedAt,
      state.ulapStatus?.checked_at,
      state.ulapStatus?.retrievedAt,
      state.ulapStatus?.retrieved_at
    );
    const summary = firstDefined(
      state.ulapStatus?.message,
      state.ulapStatus?.summary,
      state.ulapStatus?.notice
    );
    els["ulap-health-summary"].innerHTML = `
      <strong>${escapeHtml(statusMessage(rootStatus, snapshotMode ? "Local hazard data status" : "Source service status"))}</strong>
      <span>${escapeHtml(textValue(summary, checkedAt ? `Last checked ${formatDateTime(checkedAt)}` : snapshotMode ? "Import time is shown per dataset" : "Live validation time not reported"))}</span>`;

    if (!state.ulapServices.length) {
      els["ulap-service-list"].innerHTML = `<p class="muted">No validated ${snapshotMode ? "local hazard data" : "source service"} records were returned. Required hazards remain unavailable.</p>`;
      setApiStatus(kind === "danger" ? "offline" : "online", `${snapshotMode ? "Local hazard data" : "Source services"}: ${statusLabel(rootStatus)}`);
      return;
    }
    const serviceRows = state.ulapServices.map((service) => {
      const status = serviceStatus(service);
      const url = serviceLayerUrl(service);
      const layerId = firstDefined(service.layerId, service.layer_id);
      const agency = firstDefined(service.agency, service.provider, service.organization);
      const checked = firstDefined(
        service.runtime_validation?.checked_at,
        service.runtimeValidation?.checkedAt,
        service.checkedAt,
        service.checked_at,
        service.retrievedAt,
        service.retrieved_at
      );
      return `<article class="service-item">
        <div>
          <strong>${escapeHtml(serviceName(service))}</strong>
          <small>${escapeHtml([agency, layerId !== undefined ? `Layer ${layerId}` : null, checked ? `Checked ${formatDateTime(checked)}` : null].filter(Boolean).join(" · "))}</small>
        </div>
        <span class="badge ${statusKind(status)}">${escapeHtml(statusLabel(status))}</span>
        ${url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">Layer metadata</a>` : ""}
      </article>`;
    });
    const missingRequired = REQUIRED_HAZARDS.filter((definition) =>
      !state.ulapServices.some((service) => datasetType(service) === definition.key)
    );
    missingRequired.forEach((definition) => {
      serviceRows.push(`<article class="service-item">
        <div><strong>${escapeHtml(definition.label)}</strong><small>No validated ${snapshotMode ? "local data is active" : "source endpoint is configured"}.</small></div>
        <span class="badge neutral">Unavailable</span>
      </article>`);
    });
    els["ulap-service-list"].innerHTML = serviceRows.join("");
    const unavailableCount = state.ulapServices.filter((service) => !AVAILABLE_HAZARD_STATUS.has(serviceStatus(service))).length + missingRequired.length;
    setApiStatus(kind === "danger" ? "offline" : "online", unavailableCount
      ? `${snapshotMode ? "Local hazard data" : "Source services"} partial - ${unavailableCount} unavailable`
      : `${snapshotMode ? "Local hazard data" : "Data services"} available`);
  }

  function renderHazardControls() {
    const rows = MAP_HAZARDS.map((definition) => {
      const index = state.ulapServices.findIndex((service) => datasetType(service) === definition.key);
      const service = index >= 0 ? state.ulapServices[index] : null;
      const status = service ? serviceStatus(service) : "unavailable";
      const displayUrl = service ? serviceDisplayUrl(service) : null;
      const canDisplay = Boolean(displayUrl && AVAILABLE_HAZARD_STATUS.has(status));
      const url = service ? serviceLayerUrl(service) : null;
      const label = service ? serviceName(service, definition.label) : definition.label;
      const layerId = service && firstDefined(service.layerId, service.layer_id);
      const attribution = service ? serviceAttribution(service) : "No verified endpoint configured";
      const snapshotMeta = service && state.runtimeDataMode === "snapshot"
        ? [
            "Local data",
            textValue(firstDefined(service.quality_status, service.metadata?.quality_status), "").trim(),
            Number.isFinite(Number(service.feature_count)) ? `${Number(service.feature_count).toLocaleString()} features` : null
          ].filter(Boolean).join(" · ")
        : null;
      return `
        <div class="layer-service-row">
          <label class="switch-row">
            <span>
              <i class="swatch ${escapeHtml(definition.key)}"></i>
              <span>${escapeHtml(label)}
                <em class="layer-status is-${escapeHtml(statusKind(status))}">${escapeHtml(statusLabel(status))}${definition.viewOnly ? " · View only" : ""}</em>
              </span>
            </span>
            <input type="checkbox" data-service-index="${index}" ${canDisplay ? "" : "disabled"}
              aria-label="${canDisplay ? "Show" : "No verified display endpoint for"} ${escapeHtml(definition.label)} layer">
          </label>
          <details class="layer-service-details">
            <summary>Layer details</summary>
            <div class="layer-service-meta">
              <span>${escapeHtml(snapshotMeta || (layerId !== undefined && layerId !== null ? `Layer ${layerId}` : "Layer not verified"))}</span>
              <span>${escapeHtml(attribution)}</span>
              ${url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">View source information</a>` : ""}
              ${definition.viewOnly ? "<span>Optional map context · not used in scoring</span>" : ""}
              ${!canDisplay ? `<span>${displayUrl ? "Overlay disabled until service verification succeeds" : "Point query only"}</span>` : ""}
            </div>
            ${canDisplay ? `<label class="layer-opacity">Opacity
              <input type="range" min="15" max="85" value="${definition.viewOnly ? 58 : 42}" data-layer-opacity-index="${index}" aria-label="${escapeHtml(definition.label)} layer opacity">
              <output>${definition.viewOnly ? 58 : 42}%</output>
            </label>` : ""}
          </details>
        </div>`;
    });
    els["hazard-layer-controls"].innerHTML = rows.join("");
  }

  function hazardColor(type, classification, normalizedValue) {
    const rankText = textValue(classification, "").toLowerCase();
    if (type === "rain_induced_landslide") {
      if (rankText.includes("debris")) return "#111111";
      if (rankText.includes("very high")) return "#902400";
      if (rankText.includes("high")) return "#f00000";
      if (rankText.includes("moderate")) return "#008000";
      if (rankText.includes("low")) return "#ffff00";
      return "#8f3d24";
    }
    const number = toNumber(normalizedValue);
    let rank = number === null ? null : (number > 1 ? number / 100 : number);
    if (rankText.includes("very high") || rankText.includes("severe")) rank = .95;
    else if (rankText.includes("high")) rank = .8;
    else if (rankText.includes("moderate") || rankText.includes("medium")) rank = .55;
    else if (rankText.includes("low")) rank = .25;
    if (rank === null) {
      return type === "flood" ? "#3182bd" : type === "liquefaction" ? "#d9903d" : "#b74b53";
    }
    if (rank >= .8) return "#a72f3b";
    if (rank >= .6) return "#db6739";
    if (rank >= .4) return "#e5b94b";
    if (rank >= .2) return "#78a96b";
    return "#4fa89a";
  }

  function classificationDomainEntries(dataset) {
    const domain = firstDefined(
      dataset.expectedDomain,
      dataset.expected_domain,
      dataset.classificationDomain,
      dataset.classification_domain,
      dataset.codedValues,
      dataset.coded_values,
      dataset.domain,
      dataset.legend
    );
    if (Array.isArray(domain)) return domain;
    if (domain && typeof domain === "object") {
      if (Array.isArray(domain.codedValues)) return domain.codedValues;
      if (Array.isArray(domain.coded_values)) return domain.coded_values;
      return Object.entries(domain).map(([code, label]) => (
        label && typeof label === "object" ? { code, ...label } : { code, label }
      ));
    }
    return [];
  }

  function featureClassification(properties, dataset) {
    const field = firstDefined(dataset.classificationField, dataset.classification_field);
    const code = firstDefined(
      properties.rawCode,
      properties.raw_code,
      field ? properties[field] : null,
      properties.classification_code
    );
    const directLabel = firstDefined(
      properties.officialLabel,
      properties.official_label,
      properties.classificationLabel,
      properties.classification_label,
      properties.classification,
      properties.hazard_class,
      properties.category,
      properties.level
    );
    if (directLabel !== undefined && directLabel !== null) return { code, label: directLabel };
    const domainItem = classificationDomainEntries(dataset).find((item) =>
      String(firstDefined(item.code, item.value, item.id)) === String(code)
    );
    return {
      code,
      label: firstDefined(domainItem?.label, domainItem?.name, domainItem?.description, domainItem?.officialLabel)
    };
  }

  function legendColor(value, fallback) {
    if (Array.isArray(value) && value.length >= 3) {
      const [red, green, blue, alpha = 255] = value.map((part) => Number(part));
      if ([red, green, blue, alpha].every(Number.isFinite)) {
        return `rgba(${clamp(red, 0, 255)},${clamp(green, 0, 255)},${clamp(blue, 0, 255)},${clamp(alpha, 0, 255) / 255})`;
      }
    }
    if (typeof value === "string" && /^(#[0-9a-f]{3,8}|rgba?\([0-9.,%\s]+\)|[a-z]+)$/i.test(value.trim())) {
      return value.trim();
    }
    return fallback;
  }

  function arcGisExportOverlay(dataset) {
    if (!state.map || !window.L) {
      return Promise.reject(new Error("The map library is unavailable"));
    }
    const sourceUrl = serviceLayerUrl(dataset);
    if (!sourceUrl) {
      return Promise.reject(new Error("No official ArcGIS layer URL is available"));
    }
    const source = new URL(sourceUrl);
    if (source.hostname !== "ulap-hazards.georisk.gov.ph") {
      return Promise.reject(new Error("The fallback renderer only accepts the verified GeoRiskPH host"));
    }
    const layerMatch = source.pathname.match(/\/MapServer\/(\d+)\/?$/i);
    if (!layerMatch) {
      return Promise.reject(new Error("The official ArcGIS layer URL is not export-compatible"));
    }
    source.pathname = source.pathname.replace(/\/\d+\/?$/, "/export");
    source.search = "";
    const bounds = state.boundaryLayer?.getBounds()?.isValid()
      ? state.boundaryLayer.getBounds()
      : L.latLngBounds(BASEY_FALLBACK_BOUNDS);
    const width = 1600;
    const height = Math.round(clamp(
      width * ((bounds.getNorth() - bounds.getSouth()) / (bounds.getEast() - bounds.getWest())),
      700,
      2000
    ));
    source.searchParams.set("f", "image");
    source.searchParams.set("bbox", [bounds.getWest(), bounds.getSouth(), bounds.getEast(), bounds.getNorth()].join(","));
    source.searchParams.set("bboxSR", "4326");
    source.searchParams.set("imageSR", "4326");
    source.searchParams.set("size", `${width},${height}`);
    source.searchParams.set("format", "png32");
    source.searchParams.set("transparent", "true");
    source.searchParams.set("layers", `show:${layerMatch[1]}`);

    const layer = L.imageOverlay(source.href, bounds, {
      pane: "hazardOverlayPane",
      opacity: datasetType(dataset) === "rain_induced_landslide" ? .58 : .42,
      interactive: true,
      alt: `${textValue(dataset.display_name || dataset.name, "Flood")} official hazard overlay`
    });
    layer.bindPopup(
      `<strong>${escapeHtml(textValue(dataset.display_name || dataset.name, "Flood"))}</strong>` +
      `<br>Official ArcGIS map rendering` +
      `<br><small>${escapeHtml(serviceAttribution(dataset))}</small>`
    );
    layer.on("click", selectLocationFromMapEvent);
    return new Promise((resolve, reject) => {
      const timeout = window.setTimeout(() => {
        state.map?.removeLayer(layer);
        reject(new Error("The official ArcGIS map image timed out"));
      }, 15000);
      layer.once("load", () => {
        window.clearTimeout(timeout);
        resolve(layer);
      });
      layer.once("error", () => {
        window.clearTimeout(timeout);
        state.map?.removeLayer(layer);
        reject(new Error("The official ArcGIS map image could not be loaded"));
      });
      layer.addTo(state.map);
    });
  }

  async function toggleHazardLayer(dataset, checkbox) {
    const key = hazardLayerStateKey(dataset);
    if (!checkbox.checked) {
      const existing = state.hazardLayers.get(key);
      if (existing?.layer && state.map) state.map.removeLayer(existing.layer);
      state.hazardLayers.delete(key);
      renderLegend();
      return;
    }
    checkbox.disabled = true;
    let displayFailed = false;
    try {
      if (!AVAILABLE_HAZARD_STATUS.has(serviceStatus(dataset))) {
        throw new Error("The selected hazard overlay is not available");
      }
      const displayUrl = serviceDisplayUrl(dataset);
      if (!displayUrl) throw new Error("No backend-controlled Basey display endpoint is available");
      if (!state.map) throw new Error("The map library is unavailable");
      const type = datasetType(dataset);
      let geoJson = null;
      let layer = null;
      let displayMode = "features";
      const renderMode = textValue(firstDefined(
        dataset.map_render_mode,
        dataset.mapRenderMode,
        dataset.metadata?.verification?.map_render_mode
      ), "features").trim().toLowerCase();
      if (renderMode === "arcgis_export") {
        layer = await arcGisExportOverlay(dataset);
        displayMode = "arcgis_export";
      } else {
        try {
          const payload = await apiFetch(displayUrl, { timeout: 30000 });
          geoJson = parseGeoJson(payload);
          if (!geoJson?.features?.length) throw new Error("No mapped features were returned");
        } catch (error) {
          if (type !== "flood") throw error;
          layer = await arcGisExportOverlay(dataset);
          displayMode = "arcgis_export";
        }
      }
      if (!layer) {
        layer = L.geoJSON(geoJson, {
          pane: "hazardOverlayPane",
          style: (feature) => {
            const properties = feature?.properties || {};
            const classification = featureClassification(properties, dataset).label;
            return {
              color: hazardColor(type, classification),
              weight: .8,
              opacity: .95,
              fillColor: hazardColor(type, classification),
              fillOpacity: .42
            };
          },
          pointToLayer: (feature, latlng) => L.circleMarker(latlng, {
            pane: "hazardOverlayPane",
            radius: 5,
            color: hazardColor(type, featureClassification(feature?.properties || {}, dataset).label),
            fillOpacity: .7
          }),
          onEachFeature: (feature, featureLayer) => {
            const properties = feature?.properties || {};
            const classification = featureClassification(properties, dataset);
            featureLayer.bindPopup(
              `<strong>${escapeHtml(textValue(dataset.display_name || dataset.name, titleCase(type)))}</strong>` +
              `<br>Official classification: ${escapeHtml(textValue(classification.label, "Not reported"))}` +
              `${classification.code === undefined || classification.code === null ? "" : `<br>Source code: ${escapeHtml(classification.code)}`}` +
              `<br><small>${escapeHtml(serviceAttribution(dataset))}</small>`
            );
            featureLayer.on("click", selectLocationFromMapEvent);
          }
        }).addTo(state.map);
      }
      state.hazardLayers.set(key, { layer, dataset, geoJson, displayMode });
      if (displayMode === "arcgis_export") {
        const row = checkbox.closest(".layer-service-row");
        const status = row?.querySelector(".layer-status");
        if (status) status.textContent = "Available · map image";
        const metadata = row?.querySelector(".layer-service-meta");
        if (metadata && !metadata.querySelector(".overlay-render-note")) {
          metadata.insertAdjacentHTML(
            "beforeend",
            `<span class="overlay-render-note">Rendered by the official ArcGIS map service because feature export is unavailable.</span>`
          );
        }
      }
      renderLegend();
    } catch (error) {
      displayFailed = true;
      checkbox.checked = false;
      checkbox.disabled = true;
      const row = checkbox.closest(".layer-service-row");
      const status = row?.querySelector(".layer-status");
      if (status) {
        status.className = "layer-status is-danger";
        status.textContent = "Display unavailable";
      }
      const metadata = row?.querySelector(".layer-service-meta");
      if (metadata && !metadata.querySelector(".overlay-display-error")) {
        metadata.insertAdjacentHTML(
          "beforeend",
          `<span class="overlay-display-error">Overlay request failed. Reload the page to retry after the source recovers.</span>`
        );
      }
      showToast(`Could not display this hazard layer: ${error.message}`, "error");
    } finally {
      if (!displayFailed) checkbox.disabled = false;
    }
  }

  function renderLegend() {
    const active = [...state.hazardLayers.values()];
    if (!active.length) {
      els["map-legend"].innerHTML = `<h3>Legend</h3><p class="muted">Verified services without a backend display endpoint remain available for point queries only.</p>`;
      return;
    }
    const entries = [];
    for (const { dataset, geoJson } of active) {
      const type = datasetType(dataset);
      const label = textValue(firstDefined(dataset.display_name, dataset.name), titleCase(type));
      const supplied = classificationDomainEntries(dataset);
      if (supplied.length) {
        supplied.forEach((item) => {
          const itemLabel = textValue(firstDefined(item.label, item.name, item.description, item.officialLabel, item.category), "Class");
          entries.push({
            label: `${label}: ${itemLabel}`,
            color: legendColor(
              firstDefined(item.color, item.fill_color, item.symbol?.color, item.symbol?.fillColor),
              hazardColor(type, itemLabel)
            )
          });
        });
      } else {
        const classifications = [...new Set((geoJson?.features || []).map((feature) =>
          firstDefined(
            feature.properties?.classification,
            feature.properties?.hazard_class,
            feature.properties?.category,
            feature.properties?.level
          )
        ).filter((value) => value !== undefined && value !== null))];
        if (classifications.length) {
          classifications.forEach((classification) => entries.push({
            label: `${label}: ${classification}`,
            color: hazardColor(type, classification)
          }));
        } else {
          entries.push({ label, color: hazardColor(type) });
        }
      }
    }
    els["map-legend"].innerHTML = `
      <h3>Legend</h3>
      <div class="legend-items">${entries.map((item) =>
        `<span class="legend-item"><i style="background:${escapeHtml(item.color)}"></i>${escapeHtml(item.label)}</span>`
      ).join("")}</div>`;
  }

  async function loadSpatialData() {
    const tasks = await Promise.allSettled([
      loadBoundary(),
      loadBarangays(),
      loadRuntimeHazardRegistry()
    ]);
    els["map-loading"].hidden = true;
    const boundaryFailures = tasks.slice(0, 2).filter((result) => result.status === "rejected");
    const ulapFailures = tasks.slice(2).filter((result) => result.status === "rejected");
    const spatialLayers = [state.boundaryGeoJson, state.barangayGeoJson].filter(Boolean).length;
    if (boundaryFailures.length) {
      setMapNotice("data", "Some required spatial datasets are unavailable. Missing information will remain explicitly unavailable.");
    } else {
      setMapNotice("data", null);
    }
    if (ulapFailures.length) {
      state.ulapStatus = { status: "service_error", message: "Hazard data status could not be retrieved." };
      state.ulapStatusLoaded = true;
      state.ulapStatusFailed = true;
      renderUlapHealth();
      renderHazardControls();
      setMapNotice("ulap", "One or more required hazard datasets are unavailable. The score remains incomplete until the evidence is available.");
    } else {
      setMapNotice("ulap", null);
    }
    renderUlapHealth();
    els["map-data-state"].innerHTML = spatialLayers === 2
      ? `<strong>Data status:</strong> boundary (${escapeHtml(state.boundaryStatus)}), barangays (${escapeHtml(state.barangayStatus)}), hazards (${escapeHtml(state.runtimeDataMode === "snapshot" ? "Local data" : statusLabel(firstDefined(state.ulapStatus?.overallStatus, state.ulapStatus?.overall_status, state.ulapStatus?.status, "pending_verification")))})`
      : `<strong>Data status:</strong> ${spatialLayers}/2 boundary layers loaded`;
    if (state.selection && state.selection.inside === null && !state.selection.checking) {
      const identified = await identifyLocation(state.selection.latitude, state.selection.longitude);
      state.selection = {
        ...state.selection,
        inside: identified.inside,
        barangay: identified.barangay || state.selection.barangay,
        identifySource: identified.source
      };
      renderSelection();
    }
  }

  function ringContains(point, ring) {
    const [x, y] = point;
    let inside = false;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      const [xi, yi] = ring[i];
      const [xj, yj] = ring[j];
      const intersects = ((yi > y) !== (yj > y))
        && (x < ((xj - xi) * (y - yi)) / ((yj - yi) || Number.EPSILON) + xi);
      if (intersects) inside = !inside;
    }
    return inside;
  }

  function polygonContains(point, rings) {
    if (!rings?.length || !ringContains(point, rings[0])) return false;
    return !rings.slice(1).some((hole) => ringContains(point, hole));
  }

  function geometryContains(geometry, point) {
    if (!geometry) return false;
    if (geometry.type === "Polygon") return polygonContains(point, geometry.coordinates);
    if (geometry.type === "MultiPolygon") {
      return geometry.coordinates.some((polygon) => polygonContains(point, polygon));
    }
    return false;
  }

  function findContainingFeature(geoJson, latitude, longitude) {
    return geoJson?.features?.find((feature) =>
      geometryContains(feature.geometry, [longitude, latitude])
    ) || null;
  }

  function normalizeBoolean(value) {
    if (typeof value === "boolean") return value;
    if (typeof value === "number") return value === 1;
    if (typeof value === "string") {
      if (/^(true|yes|inside|within|1)$/i.test(value)) return true;
      if (/^(false|no|outside|0)$/i.test(value)) return false;
    }
    return null;
  }

  function identifyFromLocalData(latitude, longitude) {
    const boundaryFeature = findContainingFeature(state.boundaryGeoJson, latitude, longitude);
    const barangayFeature = findContainingFeature(state.barangayGeoJson, latitude, longitude);
    const canCheckBoundary = Boolean(state.boundaryGeoJson?.features?.length);
    return {
      inside: canCheckBoundary ? Boolean(boundaryFeature) : null,
      barangay: barangayFeature ? {
        id: firstDefined(barangayFeature.id, barangayFeature.properties?.id, barangayFeature.properties?.barangay_id),
        name: featureLabel(barangayFeature)
      } : null,
      source: "loaded boundary overlay",
      raw: {
        municipal_boundary: boundaryFeature?.properties || null,
        barangay: barangayFeature?.properties || null,
        notices: []
      }
    };
  }

  function normalizeBarangay(value) {
    if (!value) return null;
    if (typeof value === "string") return { name: value };
    return {
      ...value,
      id: firstDefined(value.id, value.barangay_id, value.code),
      name: textValue(firstDefined(value.name, value.barangay_name, value.label, value.properties?.name), "Barangay not reported")
    };
  }

  async function identifyLocation(latitude, longitude) {
    try {
      const query = new URLSearchParams({
        latitude: String(latitude),
        longitude: String(longitude),
        lat: String(latitude),
        lng: String(longitude),
        lon: String(longitude)
      });
      const payload = unwrap(await apiFetch(`/location/identify?${query}`)) || {};
      const boundaryRecord = firstDefined(payload.municipal_boundary, payload.boundary, payload.municipality);
      const nonLiveBoundary = boundaryRecord?.is_demo === true
        || /demo|synthetic|placeholder|mock/i.test(textValue(firstDefined(boundaryRecord?.data_status, boundaryRecord?.source_status), ""));
      if (nonLiveBoundary) {
        return {
          inside: null,
          barangay: null,
          source: "location service",
          raw: {
            ...payload,
            notices: [
              ...normalizeStringList(payload.notices),
              "The returned boundary record is not an accepted live source, so Basey containment cannot be confirmed."
            ]
          }
        };
      }
      const inside = normalizeBoolean(firstDefined(
        payload.inside_basey,
        payload.insideBasey,
        payload.inside_municipality,
        payload.within_boundary,
        payload.is_inside,
        payload.inside
      ));
      const barangay = normalizeBarangay(firstDefined(
        payload.barangay,
        payload.containing_barangay,
        payload.barangay_name
      ));
      if (inside !== null) return { inside, barangay, source: "location service", raw: payload };
    } catch {
      // The loaded, provenance-labelled boundary is a valid read-only fallback.
    }
    return identifyFromLocalData(latitude, longitude);
  }

  function markerIcon() {
    return L.divIcon({
      className: "",
      html: `<span class="selected-pin" aria-hidden="true"></span>`,
      iconSize: [30, 38],
      iconAnchor: [15, 30]
    });
  }

  function selectLocationFromMapEvent(event) {
    if (!event?.latlng) return;
    const originalEvent = event.originalEvent;
    if (originalEvent?.geosafeSelectionHandled) return;
    if (originalEvent) originalEvent.geosafeSelectionHandled = true;
    void selectLocation(event.latlng.lat, event.latlng.lng, "map click");
  }

  function resetResultView() {
    state.assessment = null;
    state.assessmentRunning = false;
    document.body.classList.remove("has-assessment", "assessment-running");
    els["results-empty"].hidden = false;
    els["results-loading"].hidden = true;
    els["results-content"].hidden = true;
    els["results-content"].innerHTML = "";
    els["result-actions"].hidden = true;
    setAssessmentStatus("neutral", "Not started");
    const runLabel = els["run-assessment"]?.querySelector("span");
    if (runLabel) runLabel.textContent = "Calculate score";
    syncMobileAssessmentAction();
    syncSelectionExperience();
  }

  function syncMobileAssessmentAction() {
    const button = els["mobile-assess"];
    const canScore = state.selection?.inside === true && !state.assessment && !state.assessmentRunning;
    if (button) {
      button.hidden = !canScore;
      button.disabled = !canScore;
    }
    if (els["mobile-view-switcher"]) {
      els["mobile-view-switcher"].hidden = !state.assessment;
    }
  }

  function showMobileWorkspaceView(view, options = {}) {
    const target = view === "map" ? document.getElementById("map-stage") : document.getElementById("results-panel");
    els["mobile-view-map"]?.setAttribute("aria-current", String(view === "map"));
    els["mobile-view-score"]?.setAttribute("aria-current", String(view === "score"));
    if (!target || options.scroll === false) return;
    target.scrollIntoView({
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
      block: "start"
    });
  }

  function renderMobileSelectionSummary() {
    const card = els["mobile-selection-summary"];
    const selection = state.selection;
    if (!card) return;
    if (!selection || state.assessment || !els["map-route-summary"]?.hidden) {
      card.hidden = true;
      return;
    }
    card.hidden = false;
    const barangay = selection.barangay?.name || selection.label || "Selected point";
    els["mobile-selection-name"].textContent = selection.checking ? "Checking location…" : barangay;
    const badge = els["mobile-selection-badge"];
    if (selection.checking) {
      badge.className = "badge neutral";
      badge.textContent = "Checking";
    } else if (selection.inside === true) {
      badge.className = "badge success";
      badge.textContent = "Inside Basey";
    } else if (selection.inside === false) {
      badge.className = "badge danger";
      badge.textContent = "Outside Basey";
    } else {
      badge.className = "badge warning";
      badge.textContent = "Unconfirmed";
    }

    if (selection.checking) {
      els["mobile-hazard-summary"].innerHTML = "<span>Confirming the selected point…</span>";
    } else if (selection.inside !== true) {
      els["mobile-hazard-summary"].innerHTML = `<span>${selection.inside === false
        ? "Choose a point inside Basey to continue."
        : "The municipal boundary could not be confirmed."}</span>`;
    } else if (!state.liveHazardResponse) {
      els["mobile-hazard-summary"].innerHTML = "<span>Checking mapped hazards…</span>";
    } else {
      els["mobile-hazard-summary"].innerHTML = pointHazards().map((hazard) => `
        <span class="mobile-hazard-item ${hazard.available ? "is-available" : "is-missing"}">
          <b>${escapeHtml(hazard.label)}</b>
          <em>${escapeHtml(hazard.available
            ? hazardClassificationPresentation(hazard).pointLabel
            : statusLabel(hazard.status))}</em>
        </span>`).join("");
    }
  }

  function syncSelectionExperience() {
    const selection = state.selection;
    document.body.classList.toggle("has-selection", Boolean(selection));
    renderMobileSelectionSummary();
    if (!els["results-empty-title"] || state.assessment || state.assessmentRunning) return;
    const action = els["empty-primary-action"];
    action.hidden = true;
    action.dataset.action = "";
    els["results-empty-steps"].hidden = false;

    if (!selection) {
      els["results-empty-title"].textContent = "Select a Basey location to begin";
      els["results-empty-copy"].textContent = "Choose a point to view its mapped hazard conditions and calculate a combined screening score.";
      return;
    }
    if (selection.checking) {
      els["results-empty-title"].textContent = "Checking the selected location";
      els["results-empty-copy"].textContent = "Confirming that the point is inside Basey before loading mapped hazard conditions.";
      els["results-empty-steps"].hidden = true;
      return;
    }
    if (selection.inside === true) {
      const barangay = selection.barangay?.name || selection.label || "this location";
      els["results-empty-title"].textContent = "Location ready for scoring";
      els["results-empty-copy"].textContent = `Review the mapped conditions for ${barangay}, then calculate the combined screening score.`;
      els["results-empty-steps"].hidden = true;
      action.hidden = false;
      action.dataset.action = "score";
      action.textContent = "Calculate score";
      return;
    }
    els["results-empty-title"].textContent = selection.inside === false
      ? "Choose a point inside Basey"
      : "Location could not be confirmed";
    els["results-empty-copy"].textContent = selection.inside === false
      ? "This point is outside the supported municipal area. Return to the Basey map and choose another location."
      : "The boundary check did not confirm this point. Try another location before calculating a score.";
    els["results-empty-steps"].hidden = true;
    action.hidden = false;
    action.dataset.action = "return";
    action.textContent = "Return to Basey";
  }

  async function selectLocation(latitude, longitude, source = "selection", label = "") {
    const lat = toNumber(latitude);
    const lng = toNumber(longitude);
    if (lat === null || lng === null || lat < -90 || lat > 90 || lng < -180 || lng > 180) {
      showToast("Enter valid latitude and longitude values.", "error");
      return;
    }

    clearRouteLayers();
    resetResultView();
    state.liveRequestSequence += 1;
    state.liveHazardResponse = null;
    state.liveHazards = new Map();
    state.liveHazardCoordinates = null;
    renderPointHazardStatus();
    state.selection = { latitude: lat, longitude: lng, source, label, checking: true };
    syncSelectionExperience();
    syncMobileAssessmentAction();
    els.latitude.value = lat.toFixed(6);
    els.longitude.value = lng.toFixed(6);
    els["inside-badge"].className = "badge neutral";
    els["inside-badge"].textContent = "Checking…";
    els["selection-summary"].innerHTML = `<p>Checking the selected point against the Basey municipal boundary…</p>`;
    els["run-assessment"].disabled = true;
    els["clear-selection"].disabled = false;
    els["search-results"].innerHTML = "";

    if (state.map) {
      if (!state.marker) {
        state.marker = L.marker([lat, lng], {
          icon: markerIcon(),
          keyboard: true,
          draggable: true,
          title: "Selected scoring point"
        }).addTo(state.map);
        state.marker.on("dragend", (event) => {
          const point = event.target.getLatLng();
          selectLocation(point.lat, point.lng, "map_click");
        });
      } else {
        state.marker.setLatLng([lat, lng]);
      }
      state.map.panTo([lat, lng]);
    }

    const identified = await identifyLocation(lat, lng);
    if (!state.selection || state.selection.latitude !== lat || state.selection.longitude !== lng) return;
    state.selection = {
      ...state.selection,
      checking: false,
      inside: identified.inside,
      barangay: identified.barangay,
      identifySource: identified.source,
      identifyRaw: identified.raw
    };
    renderSelection();
    if (state.selection.inside === true) {
      void loadLiveHazardsAtLocation(lat, lng);
    }
  }

  function renderSelection() {
    const selection = state.selection;
    if (!selection) return;
    const badge = els["inside-badge"];
    if (selection.inside === true) {
      badge.className = "badge success";
      badge.textContent = "Inside Basey";
    } else if (selection.inside === false) {
      badge.className = "badge danger";
      badge.textContent = "Outside Basey";
    } else {
      badge.className = "badge warning";
      badge.textContent = "Unconfirmed";
    }

    const barangay = selection.barangay?.name || "Not identified";
    let notice = "";
    if (selection.inside === false) {
      notice = `<div class="quality-banner danger"><span class="quality-icon"></span><div><strong>Outside the supported area</strong><p>This version of Basafe currently supports locations within Basey, Samar.</p><button class="text-button" type="button" data-return-basey>Return to the Basey map</button></div></div>`;
    } else if (selection.inside === null) {
      notice = `<div class="quality-banner"><span class="quality-icon"></span><div><strong>Boundary check unavailable</strong><p>Scoring is disabled until Basey coverage can be confirmed.</p></div></div>`;
    } else if (!selection.barangay) {
      notice = `<div class="quality-banner"><span class="quality-icon"></span><div><strong>Barangay unavailable</strong><p>The point is inside Basey, but a containing barangay was not returned.</p></div></div>`;
    }
    const identificationNotices = normalizeStringList(selection.identifyRaw?.notices);
    if (identificationNotices.length) {
      notice += `<div class="quality-banner"><span class="quality-icon"></span><div><strong>Boundary data notice</strong><p>${escapeHtml(identificationNotices.join(" "))}</p></div></div>`;
    }

    els["selection-summary"].innerHTML = `
      <dl>
        <dt>Barangay</dt><dd>${escapeHtml(barangay)}</dd>
        <dt>Latitude</dt><dd>${escapeHtml(formatCoordinate(selection.latitude))}</dd>
        <dt>Longitude</dt><dd>${escapeHtml(formatCoordinate(selection.longitude))}</dd>
      </dl>
      ${notice}`;
    els["run-assessment"].disabled = selection.inside !== true;
    syncMobileAssessmentAction();

    if (state.marker) {
      state.marker.bindPopup(
        `<strong>${escapeHtml(barangay)}</strong><br>` +
        `${escapeHtml(formatCoordinate(selection.latitude))}, ${escapeHtml(formatCoordinate(selection.longitude))}`
      ).openPopup();
    }
    syncSelectionExperience();
    syncRoutingControls();
  }

  function clearSelection() {
    clearRouteLayers();
    state.selection = null;
    state.liveRequestSequence += 1;
    state.liveHazardResponse = null;
    state.liveHazards = new Map();
    state.liveHazardCoordinates = null;
    if (state.marker && state.map) state.map.removeLayer(state.marker);
    state.marker = null;
    els.latitude.value = "";
    els.longitude.value = "";
    els["inside-badge"].className = "badge neutral";
    els["inside-badge"].textContent = "No point";
    els["selection-summary"].innerHTML = "<p>Search above or click anywhere inside Basey to place a pin.</p>";
    els["run-assessment"].disabled = true;
    syncMobileAssessmentAction();
    els["clear-selection"].disabled = true;
    renderPointHazardStatus();
    resetResultView();
    syncRoutingControls();
  }

  function clearSearchHighlight() {
    if (state.searchHighlightLayer && state.map) state.map.removeLayer(state.searchHighlightLayer);
    state.searchHighlightLayer = null;
  }

  function highlightSearchResult(result) {
    clearSearchHighlight();
    if (!state.map || !window.L || !result.geometry) return false;
    const collection = parseGeoJson(result.geometry);
    if (!collection) return false;
    state.searchHighlightLayer = L.geoJSON(collection, {
      pane: "searchResultPane",
      interactive: false,
      style: {
        color: "#ffd166", weight: 7, opacity: 1,
        fillColor: "#ffd166", fillOpacity: .2
      },
      pointToLayer: (_feature, latlng) => L.circleMarker(latlng, {
        pane: "searchResultPane", radius: 9, color: "#fff", weight: 3,
        fillColor: "#d98300", fillOpacity: 1
      })
    }).addTo(state.map);
    const bounds = state.searchHighlightLayer.getBounds?.();
    if (bounds?.isValid?.()) state.map.fitBounds(bounds.pad(.2), { maxZoom: 18 });
    return true;
  }

  function extractSearchCoordinates(item) {
    const coordinates = firstDefined(
      item.coordinates,
      item.location?.coordinates,
      item.geometry?.coordinates,
      item.center
    );
    let latitude = firstDefined(item.latitude, item.lat, item.y, item.location?.latitude);
    let longitude = firstDefined(item.longitude, item.lng, item.lon, item.x, item.location?.longitude);
    if (Array.isArray(coordinates)) {
      longitude = firstDefined(longitude, coordinates[0]);
      latitude = firstDefined(latitude, coordinates[1]);
    } else if (coordinates && typeof coordinates === "object") {
      latitude = firstDefined(latitude, coordinates.latitude, coordinates.lat);
      longitude = firstDefined(longitude, coordinates.longitude, coordinates.lng, coordinates.lon);
    }
    return { latitude: toNumber(latitude), longitude: toNumber(longitude) };
  }

  async function searchLocation(query) {
    const requestSequence = ++state.searchRequestSequence;
    els["search-results"].innerHTML = `<p class="search-feedback">Searching…</p>`;
    els["location-search"].setAttribute("aria-expanded", "true");
    try {
      const payload = await apiFetch(`/location/search?${new URLSearchParams({ q: query, limit: "16" })}`);
      if (requestSequence !== state.searchRequestSequence) return;
      const results = asArray(payload).map((item) => ({
        item,
        ...extractSearchCoordinates(item),
        kind: textValue(firstDefined(item.kind, item.type, item.result_type), "place"),
        geometry: item.geometry || null,
        label: textValue(firstDefined(item.display_name, item.name, item.label, item.address), "Search result"),
        detail: textValue(firstDefined(item.subtitle, item.barangay_name, typeof item.barangay === "string" ? item.barangay : item.barangay?.name, item.context, item.description), ""),
        dataStatus: dataStatus(firstDefined(item.municipal_boundary, item.barangay, item))
      })).filter((item) => item.latitude !== null && item.longitude !== null);

      if (!results.length) {
        els["search-results"].innerHTML = `<p class="search-feedback">No matching location was returned. Try coordinates or click the map.</p>`;
        return;
      }
      const labels = { street: "Streets", poi: "Places", place: "Places", evacuation_center: "Evacuation centers", barangay: "Barangays", coordinate: "Coordinates" };
      const groups = new Map();
      results.forEach((result, index) => {
        const group = labels[result.kind] || "Places";
        if (!groups.has(group)) groups.set(group, []);
        groups.get(group).push({ result, index });
      });
      els["search-results"].innerHTML = [...groups.entries()].map(([group, members]) => {
        const groupId = `search-group-${group.toLowerCase().replaceAll(" ", "-")}`;
        return `<div class="search-result-group" role="group" aria-labelledby="${escapeHtml(groupId)}">
          <h3 id="${escapeHtml(groupId)}">${escapeHtml(group)}</h3>
          ${members.map(({ result, index }) => `
            <button class="search-result" type="button" role="option" data-search-index="${index}" aria-label="${escapeHtml(result.label)}, ${escapeHtml(group)}">
              <span class="search-result-type" aria-hidden="true">${escapeHtml(result.kind === "evacuation_center" ? "Center" : result.kind)}</span>
              <strong>${escapeHtml(result.label)}</strong>
              <small>${escapeHtml(result.detail || `${formatCoordinate(result.latitude)}, ${formatCoordinate(result.longitude)}`)}</small>
            </button>`).join("")}
        </div>`;
      }).join("");
      els["search-results"]._results = results;
    } catch (error) {
      if (requestSequence !== state.searchRequestSequence) return;
      els["search-results"].innerHTML = `<p class="search-feedback">Search is unavailable. Enter coordinates or click the map instead.</p>`;
      showToast(`Location search failed: ${error.message}`, "error");
    }
  }

  function chooseSearchResult(result) {
    if (!result) return;
    const highlighted = highlightSearchResult(result);
    if (result.kind === "street") {
      if (!highlighted && state.map) state.map.setView([result.latitude, result.longitude], 17);
      els["search-results"].innerHTML = `
        <div class="street-selection-guidance" role="status">
          <strong>${escapeHtml(result.label)}</strong>
          <span>Street located. Select a specific point along this street to score mapped hazard conditions.</span>
        </div>`;
      showToast("Street located. Click a precise point along the highlighted road.");
      return;
    }
    if (highlighted && result.kind === "poi") showToast(`${result.label} located on the map.`);
    selectLocation(result.latitude, result.longitude, "search", result.label);
  }

  function normalizeHazardKey(value) {
    const key = textValue(value, "").toLowerCase().replace(/[^a-z0-9]+/g, "_");
    if (key.includes("liqu")) return "liquefaction";
    if (key.includes("shak") || key.includes("ground_motion")) return "ground_shaking";
    if (key.includes("flood")) return "flood";
    return key;
  }

  function objectToItems(value) {
    if (Array.isArray(value)) return value;
    if (!value || typeof value !== "object") return [];
    return Object.entries(value).map(([key, item]) =>
      item && typeof item === "object" ? { variable_name: key, ...item } : { variable_name: key, value: item }
    );
  }

  function liveHazardCollection(payload) {
    const value = unwrap(payload) || {};
    const hazards = firstDefined(
      value.hazards,
      value.hazardResults,
      value.hazard_results,
      value.results,
      Array.isArray(value) ? value : null
    );
    const map = new Map();
    objectToItems(hazards).forEach((item) => {
      const key = normalizeHazardKey(firstDefined(
        item.hazard,
        item.hazard_type,
        item.variable_name,
        item.type,
        item.name
      ));
      if (key) map.set(key, item);
    });
    return map;
  }

  function normalizeLiveHazardSource(item, definition, response = state.liveHazardResponse) {
    const sourceRecord = objectToItems(firstDefined(response?.sources, response?.sourceMetadata, response?.source_metadata)).find((source) =>
      normalizeHazardKey(firstDefined(
        source.hazard,
        source.hazard_type,
        source.variable_name,
        source.subject,
        source.dataset,
        source.name
      )) === definition.key
    );
    const itemValue = item && typeof item === "object" ? item : {};
    const nestedSource = itemValue.source && typeof itemValue.source === "object"
      ? itemValue.source
      : {};
    const nestedMetadata = nestedSource.metadata && typeof nestedSource.metadata === "object"
      ? nestedSource.metadata
      : {};
    const value = {
      ...(sourceRecord && typeof sourceRecord === "object" ? sourceRecord : {}),
      ...nestedSource,
      ...nestedMetadata,
      ...itemValue
    };
    const classification = value.classification && typeof value.classification === "object"
      ? value.classification
      : {};
    const isRejectedDemo = normalizeBoolean(firstDefined(value.is_demo, value.demonstration)) === true
      || /demo|synthetic|placeholder|mock/i.test(textValue(firstDefined(value.data_status, value.source_status), ""));
    const inferredCode = firstDefined(
      value.rawCode,
      value.raw_code,
      value.sourceCode,
      value.source_code,
      value.classificationCode,
      value.classification_code,
      classification.code
    );
    const inferredLabel = firstDefined(
      value.officialLabel,
      value.official_label,
      value.classificationLabel,
      value.classification_label,
      value.decodedLabel,
      value.decoded_label,
      classification.label,
      typeof value.classification === "string" ? value.classification : null
    );
    const returnedStatus = isRejectedDemo ? "unavailable" : normalizeStatus(firstDefined(
      value.status,
      value.availabilityStatus,
      value.availability_status,
      value.queryStatus,
      value.query_status,
      value.error?.code
    ), item ? (inferredLabel !== undefined || inferredCode !== undefined ? "available" : "invalid_response") : "unavailable");
    const status = AVAILABLE_HAZARD_STATUS.has(returnedStatus) && (inferredLabel === undefined || inferredLabel === null)
      ? "changed_schema"
      : returnedStatus;
    const cache = cacheMetadata(firstDefined(value.cache, value.cacheMetadata, value.cache_metadata));
    const spatialReference = firstDefined(
      value.spatialReference?.wkid,
      value.spatialReference,
      value.spatial_reference?.wkid,
      value.spatial_reference,
      value.sr
    );
    return {
      key: definition.key,
      label: definition.label,
      color: definition.color,
      status,
      available: AVAILABLE_HAZARD_STATUS.has(status),
      officialCode: isRejectedDemo ? null : inferredCode,
      officialLabel: isRejectedDemo ? null : inferredLabel,
      classificationField: firstDefined(
        value.classificationField,
        value.classification_field,
        value.field,
        classification.field
      ),
      agency: firstDefined(value.agency, value.provider, value.organization, value.source_name),
      service: firstDefined(value.service, value.serviceName, value.service_name, value.dataset),
      layerId: firstDefined(value.layerId, value.layer_id),
      sourceUrl: safeSourceUrl(firstDefined(value.sourceUrl, value.source_url, value.layerUrl, value.layer_url, value.url)),
      spatialReference,
      retrievedAt: firstDefined(
        value.retrievedAt,
        value.retrieved_at,
        response?.retrievedAt,
        response?.retrieved_at,
        cache.retrievedAt
      ),
      dataDate: firstDefined(
        value.dataDate,
        value.data_date,
        value.sourceDate,
        value.source_date,
        value.mappingDate,
        value.mapping_date,
        value.publicationDate,
        value.publication_date
      ),
      attribution: firstDefined(value.attribution, value.copyrightText, value.copyright_text),
      warnings: [
        ...(isRejectedDemo ? ["A demonstration hazard record was rejected. Basafe requires validated official evidence."] : []),
        ...normalizeStringList(firstDefined(value.warnings, value.notices, value.qualityWarnings, value.quality_warnings))
      ],
      cache,
      rawAttributes: firstDefined(value.rawAttributes, value.raw_attributes, value.attributes),
      statusDetail: isRejectedDemo
        ? "Validated official hazard evidence is required."
        : status === "changed_schema" && (inferredLabel === undefined || inferredLabel === null)
          ? "The live source code could not be decoded to a verified official label."
          : firstDefined(
            value.message,
            value.statusMessage,
            value.status_message,
            value.quality_notice,
            value.qualityNotice,
            value.error?.message
          )
    };
  }

  function pointHazards() {
    return REQUIRED_HAZARDS.map((definition) =>
      normalizeLiveHazardSource(state.liveHazards.get(definition.key), definition)
    );
  }

  function renderPointHazardStatus(loading = false) {
    if (loading) {
      els["ulap-point-status"].innerHTML = `<div class="point-status-loading"><span class="spinner" aria-hidden="true"></span><span>Reading local hazard classifications…</span></div>`;
      renderMobileSelectionSummary();
      return;
    }
    if (!state.liveHazardResponse) {
      els["ulap-point-status"].innerHTML = `<p class="muted">Hazard data will be checked after the point is confirmed inside Basey.</p>`;
      renderMobileSelectionSummary();
      return;
    }
    const hazards = pointHazards();
    const retrievedAt = firstDefined(state.liveHazardResponse.retrievedAt, state.liveHazardResponse.retrieved_at);
    els["ulap-point-status"].innerHTML = `
      <div class="point-status-heading">
        <strong>${escapeHtml(state.runtimeDataMode === "snapshot" ? "Local hazard data" : "Live hazard check")}</strong>
        <small>${escapeHtml(retrievedAt ? formatDateTime(retrievedAt) : "Data time not reported")}</small>
      </div>
      <div class="point-status-grid">
        ${hazards.map((hazard) => `
          <div class="point-status-row">
            <span>${escapeHtml(hazard.label)}</span>
            <span class="badge ${statusKind(hazard.status)}">${escapeHtml(statusLabel(hazard.status))}</span>
            <small>${escapeHtml(hazard.available
              ? hazardClassificationPresentation(hazard).pointLabel
              : statusMessage(hazard.status, hazard.statusDetail))}</small>
          </div>`).join("")}
      </div>
      ${hazards.some((hazard) => !hazard.available)
        ? `<p class="point-status-warning">The score remains incomplete until all three required sources respond.</p>`
        : ""}`;
    renderMobileSelectionSummary();
  }

  async function loadLiveHazardsAtLocation(latitude, longitude) {
    const sequence = ++state.liveRequestSequence;
    renderPointHazardStatus(true);
    const query = new URLSearchParams({
      latitude: String(latitude),
      longitude: String(longitude),
      lat: String(latitude),
      lon: String(longitude)
    });
    try {
      const payload = unwrap(await apiFetch(`/hazards/at-location?${query}`)) || {};
      if (sequence !== state.liveRequestSequence) return null;
      state.liveHazardResponse = payload;
      state.liveHazards = liveHazardCollection(payload);
      state.liveHazardCoordinates = { latitude, longitude };
      renderPointHazardStatus();
      return payload;
    } catch (error) {
      if (sequence !== state.liveRequestSequence) return null;
      state.liveHazardResponse = {
        hazards: {},
        retrievedAt: new Date().toISOString(),
        dataQuality: [`Hazard lookup failed: ${error.message}`],
        queryError: error.message
      };
      state.liveHazards = new Map(REQUIRED_HAZARDS.map((definition) => [
        definition.key,
        { hazard: definition.key, status: "service_error", message: error.message }
      ]));
      state.liveHazardCoordinates = { latitude, longitude };
      renderPointHazardStatus();
      return state.liveHazardResponse;
    }
  }

  function membershipsForHazard(hazard, assessment, key) {
    const embedded = objectToItems(firstDefined(
      hazard.memberships,
      hazard.membership_values,
      hazard.membershipValues,
      hazard.modelMemberships,
      hazard.model_memberships
    ));
    const membershipCollection = firstDefined(
      assessment.memberships,
      assessment.assessment_memberships,
      assessment.explanation?.memberships,
      assessment.result?.memberships,
      assessment.assessment?.memberships
    );
    const keyedMemberships = membershipCollection && typeof membershipCollection === "object"
      ? firstDefined(membershipCollection[key], membershipCollection[key.replace("_", "-")])
      : null;
    const keyed = keyedMemberships && !Array.isArray(keyedMemberships)
      ? Object.entries(keyedMemberships).map(([label, degree]) => ({ label, degree }))
      : objectToItems(keyedMemberships);
    const all = objectToItems(membershipCollection);
    const relevant = embedded.length ? embedded : all.filter((membership) => {
      const variable = normalizeHazardKey(firstDefined(
        membership.variable_name,
        membership.input_variable,
        membership.hazard_type,
        membership.variable?.name
      ));
      return variable === key;
    });
    return (embedded.length ? embedded : keyed.length ? keyed : relevant).map((membership) => ({
      label: textValue(firstDefined(
        membership.linguistic_category,
        membership.category,
        membership.membership_name,
        membership.label,
        membership.name,
        membership.term
      ), "Membership"),
      degree: toNumber(firstDefined(
        membership.degree,
        membership.membership_value,
        membership.value,
        membership.strength
      ))
    })).filter((membership) => membership.degree !== null);
  }

  function normalizeHazards(assessment) {
    const assessmentLocation = firstDefined(assessment.location, assessment.selected_location, {}) || {};
    const assessmentLatitude = toNumber(firstDefined(assessment.latitude, assessmentLocation.latitude, assessmentLocation.lat));
    const assessmentLongitude = toNumber(firstDefined(assessment.longitude, assessmentLocation.longitude, assessmentLocation.lng, assessmentLocation.lon));
    const stateLiveApplies = Boolean(
      state.liveHazardCoordinates
      && (assessmentLatitude === null || Math.abs(assessmentLatitude - state.liveHazardCoordinates.latitude) < 1e-7)
      && (assessmentLongitude === null || Math.abs(assessmentLongitude - state.liveHazardCoordinates.longitude) < 1e-7)
    );
    const rawHazards = [
      assessment.hazards,
      assessment.assessment?.hazards,
      assessment.hazard_inputs,
      assessment.inputs,
      assessment.assessment_inputs,
      assessment.result?.inputs,
      assessment.assessment?.inputs,
      assessment.modelInputs,
      assessment.model_inputs,
      assessment.transformations
    ].flatMap((collection) => objectToItems(collection));
    rawHazards.push(...objectToItems(firstDefined(
      assessment.normalizedInputs,
      assessment.normalized_inputs,
      assessment.result?.normalizedInputs,
      assessment.result?.normalized_inputs,
      assessment.assessment?.normalizedInputs,
      assessment.assessment?.normalized_inputs
    )).map((item) => ({ ...item, modelValue: firstDefined(item.modelValue, item.value) })));
    const responseHazards = liveHazardCollection(assessment);
    const indicatorWeightRecord = firstDefined(
      assessment.result?.indicator_weights,
      assessment.result?.indicatorWeights,
      assessment.assessment?.indicatorWeights,
      assessment.indicator_weights,
      {}
    ) || {};
    const indicatorWeights = firstDefined(
      indicatorWeightRecord.values,
      indicatorWeightRecord.weights,
      indicatorWeightRecord,
      {}
    ) || {};

    return REQUIRED_HAZARDS.map((definition) => {
      const matchingItems = rawHazards.filter((hazard) => normalizeHazardKey(firstDefined(
          hazard.hazard,
          hazard.hazard_type,
          hazard.variable_name,
          hazard.name,
          hazard.type,
          hazard.variable?.name
        )) === definition.key);
      const modelItem = matchingItems.length ? Object.assign({}, ...matchingItems) : null;
      const sourceItem = firstDefined(
        responseHazards.get(definition.key),
        stateLiveApplies ? state.liveHazards.get(definition.key) : null
      );
      const source = normalizeLiveHazardSource(sourceItem, definition, firstDefined(
        assessment.ulap,
        assessment.liveHazardResponse,
        assessment.live_hazard_response,
        assessment
      ));
      const transformation = firstDefined(
        modelItem?.modelTransformation,
        modelItem?.model_transformation,
        modelItem?.normalization,
        modelItem?.transformation,
        {}
      ) || {};
      const normalized = toNumber(firstDefined(
        modelItem?.modelNormalizedValue,
        modelItem?.model_normalized_value,
        modelItem?.modelValue,
        modelItem?.model_value,
        modelItem?.normalizedValue,
        modelItem?.normalized_value,
        modelItem?.normalized,
        modelItem?.numeric_value,
        modelItem?.input_value,
        transformation.normalizedValue,
        transformation.normalized_value,
        transformation.value
      ));
      const modelMissing = normalizeBoolean(firstDefined(modelItem?.missing, modelItem?.is_missing)) === true
        || normalizeBoolean(firstDefined(modelItem?.modelAvailable, modelItem?.model_available)) === false;
      const quality = [
        qualityText(firstDefined(modelItem?.data_quality, modelItem?.quality_notice, modelItem?.quality)),
        source.statusDetail,
        ...source.warnings,
        source.cache.stale ? "The cached source value is stale and must not be silently treated as current." : null
      ].filter(Boolean).join(" ");
      return {
        ...definition,
        raw: modelItem,
        live: source,
        classification: source.officialLabel,
        officialLabel: source.officialLabel,
        officialCode: source.officialCode,
        classificationField: source.classificationField,
        normalized,
        appliedWeight: toNumber(firstDefined(
          indicatorWeights[definition.key],
          indicatorWeights[definition.key.replace("_", "-")]
        )),
        weightingMethod: firstDefined(
          indicatorWeightRecord.method,
          indicatorWeightRecord.applied_method,
          "Not reported"
        ),
        source: firstDefined(source.service, source.agency, definition.label),
        agency: source.agency,
        service: source.service,
        layerId: source.layerId,
        sourceUrl: source.sourceUrl,
        spatialReference: source.spatialReference,
        attribution: source.attribution,
        retrievedAt: source.retrievedAt,
        referenceDate: source.dataDate,
        cache: source.cache,
        warnings: source.warnings,
        quality,
        status: source.available && (source.officialLabel === undefined || source.officialLabel === null)
          ? "changed_schema"
          : source.status,
        statusDetail: source.available && (source.officialLabel === undefined || source.officialLabel === null)
          ? "The live source code could not be decoded to a verified official label."
          : source.statusDetail,
        missing: !source.available || source.officialLabel === undefined || source.officialLabel === null || modelMissing || normalized === null,
        memberships: membershipsForHazard(modelItem || {}, assessment, definition.key)
      };
    });
  }

  function normalizeRules(assessment) {
    const raw = objectToItems(firstDefined(
      assessment.explanation?.activated_rules,
      assessment.explanation?.rules,
      assessment.activated_rules,
      assessment.rule_activations,
      assessment.assessment_rule_activations,
      assessment.result?.rule_activations,
      assessment.result?.activated_rules,
      assessment.assessment?.activatedRules,
      assessment.assessment?.activated_rules
    ));
    return raw.map((rule, index) => {
      const activation = toNumber(firstDefined(
        rule.activation_strength,
        rule.activationStrength,
        rule.strength,
        rule.firing_strength,
        rule.activation,
        rule.value
      ));
      return {
        id: textValue(firstDefined(rule.rule_code, rule.code, rule.rule_id, rule.ruleId, rule.id), `R${index + 1}`),
        statement: textValue(firstDefined(
          rule.statement,
          rule.ruleStatement,
          rule.rule_statement,
          rule.description,
          rule.rule?.statement,
          rule.rule?.description
        ), "Rule statement not reported"),
        activation,
        weight: toNumber(firstDefined(rule.weight, rule.ruleWeight, rule.rule_weight, rule.rule?.weight)),
        activated: normalizeBoolean(firstDefined(rule.activated, rule.is_activated))
      };
    }).filter((rule) => rule.activated !== false && (rule.activation === null || rule.activation > 0))
      .sort((a, b) => (b.activation ?? 0) - (a.activation ?? 0));
  }

  function normalizeStringList(value) {
    const list = Array.isArray(value) ? value : value ? [value] : [];
    return list.map((item) => {
      if (typeof item === "string") return item;
      const direct = firstDefined(item.message, item.text, item.notice, item.description, item.recommendation);
      if (direct !== undefined) return textValue(direct, "");
      if (item.variable || item.input || item.hazard_type) {
        const variable = titleCase(firstDefined(item.variable, item.input, item.hazard_type));
        const reason = titleCase(firstDefined(item.reason, item.status, item.availability, "Unavailable"));
        return `${variable}: ${reason}`;
      }
      return "";
    }).filter(Boolean);
  }

  function qualityText(value) {
    if (value === undefined || value === null || value === "") return null;
    if (typeof value === "string") return value;
    if (Array.isArray(value)) return value.map(qualityText).filter(Boolean).join(" ");
    if (typeof value === "object") {
      return [
        value.summary,
        value.message,
        ...normalizeStringList(value.limitations),
        ...normalizeStringList(value.validation_messages),
        value.availability ? `Availability: ${titleCase(value.availability)}` : null
      ].filter(Boolean).join(" ");
    }
    return String(value);
  }

  function normalizeIncidents(assessment) {
    return objectToItems(firstDefined(
      assessment.historical_incidents,
      assessment.incidents,
      assessment.context?.historical_incidents
    )).filter((item) => item.is_demo !== true && !/demo|synthetic|placeholder|mock/i.test(
      textValue(firstDefined(item.data_status, item.source_status), "")
    ));
  }

  function normalizeClup(assessment) {
    return objectToItems(firstDefined(
      assessment.clup_references,
      assessment.clup_context,
      assessment.context?.clup_references
    )).filter((item) => item.is_demo !== true && !/demo|synthetic|placeholder|mock/i.test(
      textValue(firstDefined(item.data_status, item.source_status), "")
    ));
  }

  function parsedObject(value) {
    if (!value) return {};
    if (typeof value === "object") return value;
    if (typeof value === "string") {
      try {
        const parsed = JSON.parse(value);
        return parsed && typeof parsed === "object" ? parsed : {};
      } catch {
        return {};
      }
    }
    return {};
  }

  function normalizedSourceEntry(entry, fallbackName, subject) {
    const value = entry && typeof entry === "object" ? entry : {};
    const metadata = parsedObject(firstDefined(value.source_metadata, value.metadata));
    const demo = firstDefined(value.is_demo, metadata.is_demo, metadata.data_classification === "demonstration");
    const official = firstDefined(value.is_official, metadata.is_official, metadata.data_classification === "official");
    const qualityNotes = firstDefined(
      value.quality_notice,
      value.data_quality,
      value.limitations,
      metadata.quality_notes
    );
    return {
      ...metadata,
      ...value,
      name: textValue(firstDefined(
        value.dataset_name,
        value.source_name,
        value.name,
        value.title,
        metadata.name,
        metadata.title
      ), fallbackName),
      provider: firstDefined(value.provider, value.organization, metadata.provider, metadata.organization),
      reference_date: firstDefined(
        value.reference_date,
        value.source_date,
        value.date,
        metadata.reference_date,
        metadata.source_date,
        metadata.date
      ),
      data_status: firstDefined(
        value.data_status,
        value.status,
        metadata.data_status,
        metadata.data_classification,
        demo === true ? "Demonstration" : null,
        official === true ? "Official" : null,
        value.quality_status,
        metadata.quality_status
      ),
      is_demo: demo === true,
      is_official: official === true,
      quality_notice: qualityText(qualityNotes),
      subject: firstDefined(subject, value.subject, value.hazard_type)
    };
  }

  function flattenSourceCatalogue(payload) {
    const value = unwrap(payload);
    if (!value) return [];
    if (Array.isArray(value)) return value.map((item) => normalizedSourceEntry(item, "Unnamed source"));
    if (typeof value !== "object") return [];
    if (Array.isArray(value.items)) {
      return value.items.map((item) => normalizedSourceEntry(item, "Unnamed source"));
    }

    const sources = [];
    objectToItems(value.municipal_boundaries).forEach((item) =>
      sources.push(normalizedSourceEntry(item, "Municipal boundary", "Municipal boundary"))
    );
    if (value.municipal_boundary) {
      sources.push(normalizedSourceEntry(value.municipal_boundary, "Municipal boundary", "Municipal boundary"));
    }
    const barangaySources = firstDefined(value.barangay_boundaries?.sources, value.barangay_boundary);
    objectToItems(barangaySources).forEach((item) =>
      sources.push(normalizedSourceEntry(item, "Barangay boundaries", "Barangay boundaries"))
    );
    objectToItems(value.hazard_datasets).forEach((item) =>
      sources.push(normalizedSourceEntry(item, titleCase(item.hazard_type || "Hazard dataset"), item.hazard_type))
    );
    objectToItems(value.historical_incident_sources).forEach((item) =>
      sources.push(normalizedSourceEntry(item, "Historical incident source", "Historical incidents"))
    );
    objectToItems(value.clup_sources).forEach((item) =>
      sources.push(normalizedSourceEntry(item, "CLUP reference source", "CLUP reference"))
    );

    if (value.historical_incidents && !Array.isArray(value.historical_incidents)) {
      sources.push(normalizedSourceEntry({
        ...value.historical_incidents,
        is_demo: value.historical_incidents.contains_demo
      }, "Historical incident catalogue", "Historical incidents"));
    }
    if (value.clup_references && !Array.isArray(value.clup_references)) {
      sources.push(normalizedSourceEntry({
        ...value.clup_references,
        is_demo: value.clup_references.contains_demo
      }, "CLUP reference catalogue", "CLUP references"));
    }
    if (value.fuzzy_model) {
      sources.push(normalizedSourceEntry(
        value.fuzzy_model,
        `Fuzzy model ${textValue(value.fuzzy_model.version, "")}`.trim(),
        "Fuzzy model"
      ));
    }
    return sources;
  }

  function normalizeSources(assessment, hazards) {
    const sources = objectToItems(firstDefined(
      assessment.data_sources,
      assessment.sources,
      assessment.provenance
    ));
    sources.push(...flattenSourceCatalogue(assessment.source_information));
    for (const hazard of hazards) {
      if (!hazard.source) continue;
      sources.push(normalizedSourceEntry({
        name: hazard.source,
        reference_date: hazard.referenceDate,
        data_status: hazard.status,
        quality_notice: hazard.quality,
        subject: hazard.label,
        agency: hazard.agency,
        provider: hazard.agency,
        service: hazard.service,
        layer_id: hazard.layerId,
        source_url: hazard.sourceUrl,
        spatial_reference: hazard.spatialReference,
        retrieved_at: hazard.retrievedAt,
        attribution: hazard.attribution,
        cache: hazard.cache,
        classification_field: hazard.classificationField
      }, hazard.label, hazard.label));
    }
    const seen = new Set();
    return sources.filter((source) => {
      const isNonLiveSource = source.is_demo === true
        || /demo|synthetic|placeholder|mock/i.test(textValue(firstDefined(source.data_status, source.status), ""));
      if (isNonLiveSource) return false;
      const key = `${firstDefined(source.id, source.name, source.title, source.dataset_name)}|${firstDefined(source.subject, source.hazard_type, "")}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }

  function assessmentFacts(assessment) {
    const result = firstDefined(
      assessment.result,
      assessment.assessment_result,
      assessment.assessment,
      Array.isArray(assessment.assessment_results) ? assessment.assessment_results[0] : null,
      assessment
    ) || {};
    const location = firstDefined(assessment.location, assessment.selected_location, {}) || {};
    const coordinates = firstDefined(location.coordinates, assessment.coordinates);
    let latitude = firstDefined(assessment.latitude, location.latitude, location.lat);
    let longitude = firstDefined(assessment.longitude, location.longitude, location.lng, location.lon);
    if (Array.isArray(coordinates)) {
      longitude = firstDefined(longitude, coordinates[0]);
      latitude = firstDefined(latitude, coordinates[1]);
    }
    latitude = firstDefined(latitude, state.selection?.latitude);
    longitude = firstDefined(longitude, state.selection?.longitude);
    const barangayValue = firstDefined(
      assessment.barangay,
      location.barangay,
      assessment.barangay_name,
      state.selection?.barangay
    );
    const barangay = typeof barangayValue === "string"
      ? barangayValue
      : firstDefined(barangayValue?.name, barangayValue?.barangay_name, barangayValue?.label);
    const score = toNumber(firstDefined(
      result.normalized_score,
      result.normalizedScore,
      result.score,
      result.vulnerability_score,
      assessment.normalized_score,
      assessment.score
    ));
    const category = firstDefined(
      result.vulnerability_category,
      result.vulnerabilityCategory,
      result.category,
      result.descriptive_category,
      assessment.vulnerability_category,
      assessment.category
    );
    const missingInputs = normalizeStringList(firstDefined(
      assessment.missing_inputs,
      result.missing_inputs,
      result.missingInputs,
      assessment.missing_data
    ));
    const explicitComplete = normalizeBoolean(firstDefined(
      assessment.is_complete,
      assessment.complete,
      result.is_complete,
      result.complete
    ));
    const status = textValue(firstDefined(
      assessment.assessmentStatus,
      assessment.assessment_status,
      assessment.status,
      result.status
    ), "");
    const reportedComplete = explicitComplete !== null
      ? explicitComplete
      : !/incomplete|missing|failed/i.test(status) && score !== null;
    const missingRequiredInput = normalizeHazards(assessment).some((hazard) => hazard.missing);
    const modelVersion = textValue(firstDefined(
      result.model_version,
      result.modelVersion,
      assessment.model_version,
      assessment.modelVersion,
      assessment.fuzzy_model?.version,
      assessment.model?.version
    ), "Not reported");
    const modelStatus = textValue(firstDefined(
      result.model_status,
      result.status_label,
      assessment.fuzzy_model?.status,
      assessment.model?.status
    ), "");
    const modelIsDemo = normalizeBoolean(firstDefined(
      result.demonstration_model,
      assessment.demonstration_model
    )) === true || /demo|not yet validated/i.test(`${modelVersion} ${modelStatus}`);
    return {
      id: firstDefined(assessment.id, assessment.assessment_id, result.assessment_id),
      latitude,
      longitude,
      barangay: textValue(barangay, "Not identified"),
      score,
      category: textValue(category, reportedComplete ? "Not categorized" : "Incomplete"),
      complete: reportedComplete && !missingRequiredInput,
      reportedComplete,
      integrityMismatch: reportedComplete && missingRequiredInput,
      missingInputs,
      modelVersion,
      modelStatus,
      modelIsDemo,
      createdAt: firstDefined(assessment.createdAt, assessment.created_at, assessment.assessed_at, assessment.timestamp),
      disclaimer: DISCLAIMER
    };
  }

  async function hydrateAssessment(payload) {
    let assessment = unwrap(payload) || {};
    const initialId = firstDefined(assessment.id, assessment.assessment_id);
    const hasDetail = firstDefined(
      assessment.result,
      assessment.assessment_result,
      assessment.normalized_score,
      assessment.hazards,
      assessment.inputs
    ) !== undefined;
    if (initialId && !hasDetail) {
      const detail = unwrap(await apiFetch(`/assessments/${encodeURIComponent(initialId)}`));
      if (detail) assessment = detail;
    }
    const facts = assessmentFacts(assessment);
    const query = new URLSearchParams({
      latitude: String(facts.latitude ?? state.selection?.latitude ?? ""),
      longitude: String(facts.longitude ?? state.selection?.longitude ?? ""),
      lat: String(facts.latitude ?? state.selection?.latitude ?? ""),
      lng: String(facts.longitude ?? state.selection?.longitude ?? ""),
      lon: String(facts.longitude ?? state.selection?.longitude ?? "")
    });
    const [explanationPayload, incidentsPayload, clupPayload, sourcesPayload] = await Promise.all([
      facts.id ? optionalFetch(`/assessments/${encodeURIComponent(facts.id)}/explanation`) : null,
      optionalFetch(`/incidents/nearby?${query}`),
      optionalFetch(`/clup-references/by-location?${query}`),
      optionalFetch("/data-sources")
    ]);
    const explanation = unwrap(explanationPayload);
    if (explanation && !assessment.explanation) assessment.explanation = explanation;
    if (!normalizeIncidents(assessment).length && incidentsPayload) {
      assessment.historical_incidents = asArray(incidentsPayload);
    }
    if (!normalizeClup(assessment).length && clupPayload) {
      assessment.clup_references = asArray(clupPayload);
    }
    if (!objectToItems(firstDefined(assessment.data_sources, assessment.sources)).length && sourcesPayload) {
      assessment.data_sources = flattenSourceCatalogue(sourcesPayload);
    }
    return assessment;
  }

  function setAssessmentStatus(kind, label) {
    els["assessment-status"].className = `badge ${kind}`;
    els["assessment-status"].textContent = label;
  }

  function qualityNotices(assessment, hazards, facts) {
    const values = [
      ...normalizeStringList(firstDefined(
        assessment.data_quality_notices,
        assessment.dataQuality,
        assessment.quality_notices,
        assessment.notices,
        assessment.assessment?.dataQuality
      )),
      ...normalizeStringList(firstDefined(
        state.liveHazardResponse?.dataQuality,
        state.liveHazardResponse?.data_quality,
        state.liveHazardResponse?.warnings
      )),
      ...hazards.map((hazard) => hazard.quality).filter(Boolean)
    ];
    if (!facts.complete) {
      values.unshift("This score is incomplete. Missing information has not been interpreted as low vulnerability.");
    }
    if (hazards.some((hazard) => hazard.missing)) {
      values.push("At least one required hazard input is unavailable.");
    }
    hazards.filter((hazard) => !AVAILABLE_HAZARD_STATUS.has(normalizeStatus(hazard.status))).forEach((hazard) => {
      values.push(`${hazard.label}: ${statusMessage(hazard.status, hazard.statusDetail)}.`);
    });
    hazards.filter((hazard) => hazard.cache?.stale).forEach((hazard) => {
      values.push(`${hazard.label}: cached source data is stale; a complete current score must not rely on it silently.`);
    });
    if (facts.integrityMismatch) {
      values.unshift("Result integrity warning: a score was returned even though a required verified hazard input is unavailable. The interface has suppressed that score and disabled PDF download.");
    }
    return [...new Set(values.map(String))];
  }

  function statusTag(source) {
    const rawStatus = textValue(firstDefined(
      source.data_status,
      source.status,
      source.provenance_status,
      source.is_official === true ? "Official" : null
    ), "Status not reported");
    if (/official|authoritative/i.test(rawStatus)) {
      return `<span class="badge success source-tag">Official</span>`;
    }
    const status = normalizeStatus(rawStatus, "pending_verification");
    return `<span class="badge ${statusKind(status)} source-tag">${escapeHtml(statusLabel(status))}</span>`;
  }

  function buildScoreCard(facts) {
    const score = !facts.complete || facts.score === null ? "—" : Math.round(facts.score);
    const ringValue = !facts.complete || facts.score === null ? 0 : clamp(facts.score, 0, 100);
    return `
      <section class="score-card ${facts.complete ? "" : "incomplete"}" aria-label="Preliminary multi-hazard screening result">
        <div class="score-main">
          <div class="score-ring" style="--score:${ringValue}">
            <span class="score-value">${escapeHtml(score)}</span>
          </div>
          <div class="score-label">
            <p>${facts.complete ? "Multi-hazard score · 0–100" : "Scoring status"}</p>
            <h3>${escapeHtml(facts.complete ? facts.category : "Incomplete")}</h3>
            <small>${facts.complete
              ? "Preliminary screening result"
              : `No three-hazard score is shown until every required verified input is available.`}</small>
          </div>
        </div>
        <p class="score-caution">Preliminary screening only—not a safety certification.</p>
      </section>`;
  }

  function buildInterpretationSummary(facts, hazards) {
    if (!facts.complete) return "";
    const available = hazards.filter((hazard) => !hazard.missing && hazard.normalized !== null);
    if (!available.length) return "";
    const leading = available.reduce((current, hazard) => {
      const currentValue = current.normalized * (current.appliedWeight ?? 1);
      const hazardValue = hazard.normalized * (hazard.appliedWeight ?? 1);
      return hazardValue > currentValue ? hazard : current;
    });
    const classification = hazardClassificationPresentation(leading).label;
    return `<section class="result-interpretation" aria-label="Plain-language score interpretation">
      <span>Main mapped condition in this score</span>
      <strong>${escapeHtml(leading.label)} · ${escapeHtml(classification)}</strong>
      <p>This is the largest normalized model input for this result. It is not a standalone safety rating or proof that one hazard caused the final score.</p>
    </section>`;
  }

  function buildHazards(hazards) {
    return `
      <section class="result-section">
        <h3>Mapped hazard conditions <span class="badge info">Source evidence</span></h3>
        <div class="hazard-grid">
          ${hazards.map((hazard) => {
            const sourceAvailable = hazard.live?.available === true;
            const classification = hazardClassificationPresentation(hazard);
            const cache = hazard.cache || cacheMetadata({});
            const serviceLabel = [
              hazard.agency,
              hazard.service,
              hazard.layerId !== undefined && hazard.layerId !== null ? `Layer ${hazard.layerId}` : null
            ].filter(Boolean).join(" · ");
            return `
              <article class="hazard-card ${hazard.missing ? "is-missing" : ""}" style="--hazard-color:${hazard.color}">
                <div class="hazard-card-heading">
                  <span class="hazard-name">${escapeHtml(hazard.label)}</span>
                  <span class="badge ${statusKind(hazard.status)}">${escapeHtml(statusLabel(hazard.status))}</span>
                </div>

                <div class="official-source-value">
                  <span class="value-label">Mapped source classification</span>
                  <strong>${escapeHtml(sourceAvailable
                    ? classification.label
                    : "Unavailable")}</strong>
                  ${!sourceAvailable
                    ? `<p>${escapeHtml(statusMessage(hazard.status, hazard.statusDetail))}</p>`
                    : ""}
                  ${sourceAvailable && classification.note
                    ? `<p class="classification-note">${escapeHtml(classification.note)}</p>`
                    : ""}
                </div>
                <p class="hazard-source-line"><strong>Source:</strong> ${escapeHtml(textValue(hazard.agency, hazard.service || "Not reported"))}</p>

                <details class="hazard-details">
                  <summary>View source and technical details</summary>
                  <div class="model-transform-value">
                    <span class="value-label">Basafe normalized input (0–1)</span>
                    <strong>${hazard.normalized === null
                      ? "Not calculated"
                      : escapeHtml(formatValue(hazard.normalized / 100, 3))}</strong>
                    <small>Model index ${hazard.normalized === null ? "not calculated" : `${escapeHtml(formatValue(hazard.normalized, 2))} / 100`}; not an official agency numerical rating.</small>
                  </div>
                  <dl class="hazard-provenance">
                    ${hazard.officialCode !== undefined && hazard.officialCode !== null
                      ? `<dt>Source code</dt><dd>${escapeHtml(textValue(hazard.classificationField, "Classification"))}: ${escapeHtml(hazard.officialCode)}</dd>`
                      : ""}
                    <dt>Agency / service</dt><dd>${escapeHtml(textValue(serviceLabel, "Not reported"))}</dd>
                    <dt>Source date</dt><dd>${escapeHtml(formatDate(hazard.referenceDate))}${hazard.referenceDate ? "" : " · not reported"}</dd>
                    <dt>Retrieved / imported</dt><dd>${escapeHtml(formatDateTime(hazard.retrievedAt))}</dd>
                    <dt>Applied indicator weight</dt><dd>${hazard.appliedWeight === null ? "Not calculated" : escapeHtml(formatValue(hazard.appliedWeight, 3))} · ${escapeHtml(titleCase(hazard.weightingMethod))}</dd>
                    <dt>Spatial reference</dt><dd>${escapeHtml(textValue(hazard.spatialReference))}</dd>
                    <dt>Local copy / cache</dt><dd>${escapeHtml(titleCase(cache.source))}${cache.expiresAt ? ` · expires ${escapeHtml(formatDateTime(cache.expiresAt))}` : ""}${cache.stale ? ` <span class="badge danger">Stale</span>` : ""}</dd>
                  </dl>
                  ${hazard.sourceUrl
                    ? `<a class="source-url" href="${escapeHtml(hazard.sourceUrl)}" target="_blank" rel="noopener noreferrer">Open official layer metadata</a>`
                    : `<span class="source-url unavailable">Layer URL not reported</span>`}
                  ${hazard.attribution ? `<p class="hazard-attribution">${escapeHtml(hazard.attribution)}</p>` : ""}
                </details>
                ${hazard.warnings?.length
                  ? `<ul class="hazard-warning-list">${hazard.warnings.map((warning) => `<li>${escapeHtml(warning)}</li>`).join("")}</ul>`
                  : ""}
              </article>`;
          }).join("")}
        </div>
      </section>`;
  }

  function buildMemberships(hazards) {
    const available = hazards.filter((hazard) => hazard.memberships.length);
    return `
      <section class="result-section">
        <h3>Basafe fuzzy memberships <span class="badge neutral">Internal 0–1</span></h3>
        ${available.length ? `<div class="memberships">
          ${available.map((hazard) => `
            <div class="membership-group">
              <strong>${escapeHtml(hazard.label)}</strong>
              ${hazard.memberships.map((membership) => {
                const degree = clamp(membership.degree);
                return `<div class="membership-row">
                  <span>${escapeHtml(membership.label)}</span>
                  <span class="membership-track" aria-hidden="true"><i class="membership-fill" style="width:${degree * 100}%"></i></span>
                  <span class="membership-value">${degree.toFixed(2)}</span>
                </div>`;
              }).join("")}
            </div>`).join("")}
        </div>` : `<p class="muted">No model memberships were calculated. Official source classifications above remain unchanged; missing model values do not imply low vulnerability.</p>`}
      </section>`;
  }

  function buildRules(rules) {
    return `
      <section class="result-section">
        <h3>Activated fuzzy rules <span class="badge neutral">${rules.length} contributing</span></h3>
        ${rules.length ? `<div class="data-table-wrap"><table class="data-table">
          <caption class="visually-hidden">Activated fuzzy rules and strengths</caption>
          <thead><tr><th>Rule</th><th>Statement</th><th>Activation</th></tr></thead>
          <tbody>${rules.map((rule) => {
            const strength = rule.activation === null ? null : clamp(rule.activation);
            return `<tr>
              <td><strong>${escapeHtml(rule.id)}</strong>${rule.weight === null ? "" : `<br><small>Weight ${escapeHtml(formatValue(rule.weight, 2))}</small>`}</td>
              <td>${escapeHtml(rule.statement)}</td>
              <td class="strength-cell">
                <span class="strength-number">${strength === null ? "Not reported" : strength.toFixed(2)}</span>
                ${strength === null ? "" : `<span class="strength-track" aria-hidden="true"><i style="width:${strength * 100}%"></i></span>`}
              </td>
            </tr>`;
          }).join("")}</tbody>
        </table></div>` : `<p class="muted">No activated-rule explanation was returned. Do not treat this as evidence that no rule contributed.</p>`}
      </section>`;
  }

  function buildContext(incidents, clup) {
    const incidentHtml = incidents.length ? incidents.map((incident) => {
      const sourceMetadata = parsedObject(incident.source_metadata);
      const title = firstDefined(incident.title, incident.incident_type, incident.type, incident.name);
      const description = firstDefined(incident.description, incident.summary, incident.notes);
      const date = firstDefined(incident.incident_date, incident.date, incident.occurred_at, sourceMetadata.date);
      const distance = toNumber(firstDefined(incident.distance_km, incident.distance));
      const severity = firstDefined(incident.severity, incident.severity_class);
      const relationship = firstDefined(incident.match_reason, incident.relationship);
      const status = incident.is_official === true ? "Official" : firstDefined(incident.data_status, incident.source_status);
      return `<li class="context-item">
        <strong>${escapeHtml(textValue(title, "Historical incident"))}</strong>
        <p>${escapeHtml(textValue(description, "No description reported."))}</p>
        <div class="context-meta"><span>${escapeHtml(formatDate(date))}</span>${severity ? `<span>Severity: ${escapeHtml(textValue(severity))}</span>` : ""}${relationship ? `<span>${escapeHtml(titleCase(relationship))}</span>` : ""}${distance === null ? "" : `<span>${escapeHtml(formatValue(distance, 2))} km away</span>`}${status ? `<span class="badge ${/official|verified|available/i.test(status) ? "success" : "neutral"}">${escapeHtml(titleCase(status))}</span>` : ""}</div>
      </li>`;
    }).join("") : `<li class="context-item"><p>No relevant incident records were returned. This does not mean no incidents occurred.</p></li>`;

    const clupHtml = clup.length ? clup.map((reference) => {
      const sourceMetadata = parsedObject(reference.source_metadata);
      const title = firstDefined(reference.title, reference.reference_name, reference.name, reference.zone_name, reference.classification);
      const description = firstDefined(reference.description, reference.notes, reference.context, reference.land_use);
      const planningNote = reference.planning_note;
      const documentSection = firstDefined(reference.document_section, reference.section);
      const referenceType = firstDefined(reference.reference_type, reference.type);
      const date = firstDefined(reference.reference_date, reference.adoption_date, reference.date, sourceMetadata.source_date, sourceMetadata.date);
      const relationship = firstDefined(reference.match_reason, reference.relationship);
      const status = reference.is_official === true ? "Official" : firstDefined(reference.data_status, reference.source_status);
      return `<li class="context-item">
        <strong>${escapeHtml(textValue(title, "CLUP reference"))}</strong>
        <p>${escapeHtml(textValue(description, "No contextual description reported."))}</p>
        ${planningNote ? `<p><strong>Planning note:</strong> ${escapeHtml(textValue(planningNote))}</p>` : ""}
        <div class="context-meta"><span>${escapeHtml(formatDate(date))}</span>${referenceType ? `<span>${escapeHtml(titleCase(referenceType))}</span>` : ""}${documentSection ? `<span>Section: ${escapeHtml(textValue(documentSection))}</span>` : ""}${relationship ? `<span>${escapeHtml(titleCase(relationship))}</span>` : ""}${status ? `<span class="badge ${/official|verified|available/i.test(status) ? "success" : "neutral"}">${escapeHtml(titleCase(status))}</span>` : ""}</div>
      </li>`;
    }).join("") : `<li class="context-item"><p>No location-specific CLUP reference was returned. Verify the adopted official plan before planning use.</p></li>`;

    return `
      <section class="result-section">
        <h3>Historical incident context</h3>
        <ul class="context-list">${incidentHtml}</ul>
      </section>
      <section class="result-section">
        <h3>CLUP reference context</h3>
        <ul class="context-list">${clupHtml}</ul>
        <p class="context-caution">CLUP references are planning context only. This screening result is not a legal zoning or land-use determination.</p>
      </section>`;
  }

  function buildSources(sources) {
    return `
      <section class="result-section">
        <h3>Sources, dates &amp; status</h3>
        ${sources.length ? `<ul class="source-list">${sources.map((source) => {
          const name = textValue(firstDefined(source.name, source.title, source.dataset_name, source.provider), "Unnamed source");
          const provider = firstDefined(source.provider, source.agency, source.organization);
          const date = firstDefined(source.reference_date, source.source_date, source.updated_at, source.date);
          const subject = firstDefined(source.subject, source.hazard_type, source.data_type);
          const sourceUrl = safeSourceUrl(firstDefined(source.source_url, source.sourceUrl, source.layer_url, source.layerUrl, source.url));
          const retrievedAt = firstDefined(source.retrieved_at, source.retrievedAt);
          const layerId = firstDefined(source.layer_id, source.layerId);
          const spatialReference = firstDefined(source.spatial_reference, source.spatialReference);
          const classificationField = firstDefined(source.classification_field, source.classificationField);
          const cache = cacheMetadata(source.cache);
          const quality = qualityText(firstDefined(
            source.quality_notice,
            source.quality_notes,
            source.data_quality,
            source.limitations
          ));
          const metadata = [
            subject && titleCase(subject),
            provider,
            layerId !== undefined && layerId !== null ? `Layer ${layerId}` : null,
            `Data date: ${formatDate(date)}`,
            retrievedAt ? `Retrieved: ${formatDateTime(retrievedAt)}` : null,
            spatialReference ? `SR: ${textValue(spatialReference)}` : null,
            classificationField ? `Field: ${classificationField}` : null,
            cache.source !== "not reported" ? `Cache: ${titleCase(cache.source)}${cache.stale ? " (stale)" : ""}` : null
          ].filter(Boolean).join(" · ");
          return `<li>
            <strong>${escapeHtml(name)}</strong>
            <small>${escapeHtml(metadata)}${source.attribution ? `<br>${escapeHtml(source.attribution)}` : ""}${quality ? `<br>${escapeHtml(quality)}` : ""}${sourceUrl ? `<br><a href="${escapeHtml(sourceUrl)}" target="_blank" rel="noopener noreferrer">Official layer metadata</a>` : ""}</small>
            ${statusTag(source)}
          </li>`;
        }).join("")}</ul>` : `<p class="muted">No data-source register was returned. Source provenance is required before operational interpretation.</p>`}
      </section>`;
  }

  function buildResultHtml(assessment) {
    const facts = assessmentFacts(assessment);
    const hazards = normalizeHazards(assessment);
    const rules = normalizeRules(assessment);
    const incidents = normalizeIncidents(assessment);
    const clup = normalizeClup(assessment);
    const sources = normalizeSources(assessment, hazards);
    const notices = qualityNotices(assessment, hazards, facts);
    const recommendations = normalizeStringList(firstDefined(
      assessment.recommendations,
      assessment.planning_recommendations,
      assessment.result?.recommendations,
      assessment.assessment?.recommendations
    ));
    const missingByKey = new Map();
    [...facts.missingInputs, ...hazards.filter((hazard) => hazard.missing).map((hazard) => hazard.key)].forEach((value) => {
      const key = normalizeHazardKey(value);
      const definition = REQUIRED_HAZARDS.find((hazard) => hazard.key === key);
      const label = definition?.label || titleCase(value);
      missingByKey.set(key || label.toLowerCase(), label);
    });
    const missing = [...missingByKey.values()];

    return `
      ${buildScoreCard(facts)}
      ${buildInterpretationSummary(facts, hazards)}
      ${!facts.complete ? `<div class="quality-banner danger">
        <span class="quality-icon"></span>
        <div><strong>Required information is missing</strong><p>${missing.length
          ? `Unavailable: ${escapeHtml(missing.join(", "))}.`
          : "The scoring service marked this result incomplete."} Missing data is not low vulnerability.</p></div>
      </div>` : ""}
      <section class="result-section result-location-bar">
        <div class="location-result-grid">
          <div class="result-fact"><span>Barangay</span><strong>${escapeHtml(facts.barangay)}</strong></div>
          <div class="result-fact"><span>Coordinates</span><strong>${escapeHtml(formatCoordinate(facts.latitude))}, ${escapeHtml(formatCoordinate(facts.longitude))}</strong></div>
        </div>
      </section>

      <div class="result-tabs" id="result-tabs">
        <nav class="result-tab-nav" role="tablist" aria-label="Score sections">
          <button class="result-tab active" role="tab" tabindex="0"  aria-selected="true"  aria-controls="rtab-hazards" id="rtab-btn-hazards">Hazards</button>
          <button class="result-tab"        role="tab" tabindex="-1" aria-selected="false" aria-controls="rtab-model"   id="rtab-btn-model" aria-label="Why this score">Why</button>
          <button class="result-tab"        role="tab" tabindex="-1" aria-selected="false" aria-controls="rtab-context" id="rtab-btn-context" aria-label="Local CDRA and CLUP context">Context</button>
          <button class="result-tab"        role="tab" tabindex="-1" aria-selected="false" aria-controls="rtab-details" id="rtab-btn-details" aria-label="Sources and limitations">Sources</button>
        </nav>

        <div class="result-tab-panels">

          <!-- TAB 1: HAZARDS -->
          <div class="result-tab-panel active" id="rtab-hazards" role="tabpanel" aria-labelledby="rtab-btn-hazards">
            ${buildHazards(hazards)}
          </div>

          <!-- TAB 2: MODEL -->
          <div class="result-tab-panel" id="rtab-model" role="tabpanel" aria-labelledby="rtab-btn-model" hidden>
            ${buildMemberships(hazards)}
            ${buildRules(rules)}
          </div>

          <!-- TAB 3: CDRA / CLUP CONTEXT -->
          <div class="result-tab-panel" id="rtab-context" role="tabpanel" aria-labelledby="rtab-btn-context" hidden>
            ${buildContext(incidents, clup)}
          </div>

          <!-- TAB 4: DETAILS (Sources, Notices, Recs, Disclaimer) -->
          <div class="result-tab-panel" id="rtab-details" role="tabpanel" aria-labelledby="rtab-btn-details" hidden>
            <section class="result-section">
              <h3>Data-quality &amp; availability notices</h3>
              ${notices.length
                ? `<ul class="notice-list">${notices.map((notice) => `<li>${escapeHtml(notice)}</li>`).join("")}</ul>`
                : `<p class="muted">No quality notices were returned. Consult each source record before interpreting the result.</p>`}
            </section>
            <section class="result-section">
              <h3>Planning-oriented recommendations</h3>
              ${recommendations.length
                ? `<ul class="recommendation-list">${recommendations.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`
                : `<p class="muted">No location-specific recommendation was returned. Verify the evidence with qualified municipal and technical professionals.</p>`}
            </section>
            ${buildSources(sources)}
            <section class="result-section">
              <h3>Disclaimer</h3>
              <div class="inline-disclaimer">${escapeHtml(facts.disclaimer)}</div>
            </section>
          </div>

        </div>
      </div>`;
  }

  function renderAssessment(assessment) {
    state.assessment = assessment;
    state.assessmentRunning = false;
    document.body.classList.remove("assessment-running");
    document.body.classList.add("has-assessment");
    const facts = assessmentFacts(assessment);
    els["results-empty"].hidden = true;
    els["results-loading"].hidden = true;
    els["results-content"].innerHTML = buildResultHtml(assessment);
    els["results-content"].hidden = false;
    els["result-actions"].hidden = false;
    setAssessmentStatus(facts.complete ? "success" : "warning", facts.complete ? "Inputs complete" : "Incomplete");
    const runLabel = els["run-assessment"]?.querySelector("span");
    if (runLabel) runLabel.textContent = "Recalculate score";
    syncMobileAssessmentAction();
    els["download-report"].disabled = !facts.id || facts.integrityMismatch;
    els["download-report-dialog"].disabled = !facts.id || facts.integrityMismatch;
    saveRecent(assessment);
    renderHistory();
    renderMobileSelectionSummary();
    showMobileWorkspaceView("score");
    // Wire tab switching (script tags don't execute when injected via innerHTML)
    wireResultTabs();
  }

  function wireResultTabs() {
    var container = document.getElementById("result-tabs");
    if (!container) return;
    var tabs = container.querySelectorAll(".result-tab");
    var panels = container.querySelectorAll(".result-tab-panel");
    function activateTab(tab, moveFocus) {
        var targetId = tab.getAttribute("aria-controls");
        tabs.forEach(function (t) { t.classList.remove("active"); t.setAttribute("aria-selected", "false"); });
        tabs.forEach(function (t) { t.setAttribute("tabindex", "-1"); });
        panels.forEach(function (p) { p.classList.remove("active"); p.hidden = true; });
        tab.classList.add("active");
        tab.setAttribute("aria-selected", "true");
        tab.setAttribute("tabindex", "0");
        var panel = document.getElementById(targetId);
        if (panel) { panel.classList.add("active"); panel.hidden = false; }
        if (moveFocus) tab.focus();
    }
    tabs.forEach(function (tab, index) {
      tab.addEventListener("click", function () {
        activateTab(this, false);
      });
      tab.addEventListener("keydown", function (event) {
        var nextIndex = null;
        if (event.key === "ArrowRight") nextIndex = (index + 1) % tabs.length;
        else if (event.key === "ArrowLeft") nextIndex = (index - 1 + tabs.length) % tabs.length;
        else if (event.key === "Home") nextIndex = 0;
        else if (event.key === "End") nextIndex = tabs.length - 1;
        if (nextIndex === null) return;
        event.preventDefault();
        activateTab(tabs[nextIndex], true);
      });
    });
  }

  async function runAssessment() {
    const selection = state.selection;
    if (!selection || selection.inside !== true) {
      showToast("Select and confirm a point inside Basey first.", "error");
      return;
    }
    state.assessmentRunning = true;
    document.body.classList.add("assessment-running");
    syncMobileAssessmentAction();
    els["run-assessment"].disabled = true;
    els["results-empty"].hidden = true;
    els["results-content"].hidden = true;
    els["result-actions"].hidden = true;
    els["results-loading"].hidden = false;
    setAssessmentStatus("info", "Evaluating");
    setProgress("location");

    const payload = {
      latitude: selection.latitude,
      longitude: selection.longitude,
      coordinates: {
        latitude: selection.latitude,
        longitude: selection.longitude
      },
      location: {
        type: "Point",
        coordinates: [selection.longitude, selection.latitude],
        latitude: selection.latitude,
        longitude: selection.longitude,
        selection_method: selection.source === "search"
          ? "search"
          : selection.source === "coordinate input" ? "coordinates" : "map_click",
        display_label: selection.label || null
      },
      barangay_id: selection.barangay?.id ?? null,
      location_label: selection.label || null
    };

    try {
      setProgress("layers");
      const livePayload = await loadLiveHazardsAtLocation(selection.latitude, selection.longitude);
      setProgress("classifications");
      setProgress("completeness");
      setProgress("calculation");
      const created = await apiFetch("/assessments", {
        method: "POST",
        body: JSON.stringify(payload),
        timeout: 30000
      });
      setProgress("explanation");
      const assessment = await hydrateAssessment(created);
      if (livePayload && !assessment.ulap) assessment.ulap = livePayload;
      setProgress("report");
      renderAssessment(assessment);
    } catch (error) {
      els["results-loading"].hidden = true;
      const fallbackHazards = state.liveHazardResponse?.hazards || Object.fromEntries(
        REQUIRED_HAZARDS.map((definition) => [
          definition.key,
          state.liveHazards.get(definition.key) || {
            hazard: definition.key,
            status: "service_error",
            message: "The current hazard query was not completed."
          }
        ])
      );
      const fallback = {
        location: {
          latitude: selection.latitude,
          longitude: selection.longitude,
          barangay: selection.barangay
        },
        hazards: fallbackHazards,
        assessment: {
          status: "incomplete",
          score: null,
          category: null,
          missingInputs: REQUIRED_HAZARDS
            .filter((definition) => !normalizeLiveHazardSource(state.liveHazards.get(definition.key), definition).available)
            .map((definition) => definition.key),
          memberships: {},
          activatedRules: [],
          recommendations: []
        },
        dataQuality: [
          `Scoring service error: ${error.message}`,
          "A complete score has not been generated. Missing or unavailable information is not low vulnerability."
        ],
        disclaimer: DISCLAIMER,
        ulap: state.liveHazardResponse
      };
      renderAssessment(fallback);
      setAssessmentStatus("warning", "Incomplete");
      showToast(`Score could not be generated: ${error.message}`, "error");
    } finally {
      state.assessmentRunning = false;
      document.body.classList.remove("assessment-running");
      els["run-assessment"].disabled = state.selection?.inside !== true;
      syncMobileAssessmentAction();
    }
  }

  function setProgress(activeStep) {
    const order = ["location", "layers", "classifications", "completeness", "calculation", "explanation", "report"];
    const activeIndex = order.indexOf(activeStep);
    document.querySelectorAll("[data-progress-step]").forEach((item) => {
      const index = order.indexOf(item.dataset.progressStep);
      item.classList.toggle("is-active", index === activeIndex);
      item.classList.toggle("is-complete", index >= 0 && index < activeIndex);
    });
    const message = document.querySelector("#results-loading > p");
    const current = document.querySelector(`[data-progress-step="${activeStep}"]`);
    if (message && current) message.textContent = `${current.textContent}...`;
  }

  function mobileControlsEnabled() {
    return window.matchMedia("(max-width: 760px)").matches;
  }

  function syncControlsAccessibility() {
    const rail = document.getElementById("assessment-controls");
    if (!rail) return;
    const mobile = mobileControlsEnabled();
    if (!mobile) rail.classList.remove("is-open");
    const open = mobile && rail.classList.contains("is-open");
    rail.toggleAttribute("inert", mobile && !open);
    if (mobile) rail.setAttribute("aria-hidden", String(!open));
    else rail.removeAttribute("aria-hidden");
    els["open-controls"]?.setAttribute("aria-expanded", String(open));
    if (els["controls-backdrop"]) els["controls-backdrop"].hidden = !open;
    document.body.classList.toggle("controls-open", open);
  }

  function setControlsOpen(open, options = {}) {
    const rail = document.getElementById("assessment-controls");
    if (!rail) return;
    const shouldOpen = mobileControlsEnabled() && open;
    if (shouldOpen) controlsReturnFocus = document.activeElement;
    rail.classList.toggle("is-open", shouldOpen);
    syncControlsAccessibility();
    if (shouldOpen) {
      window.requestAnimationFrame(() => els["close-controls"]?.focus());
    } else if (options.restoreFocus !== false && mobileControlsEnabled()) {
      const target = controlsReturnFocus?.isConnected ? controlsReturnFocus : els["open-controls"];
      target?.focus();
      controlsReturnFocus = null;
    }
  }

  function keepFocusInsideControls(event) {
    if (event.key !== "Tab" || !document.body.classList.contains("controls-open")) return;
    const rail = document.getElementById("assessment-controls");
    const focusable = Array.from(rail?.querySelectorAll(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])'
    ) || []).filter((element) => element.getClientRects().length && !element.closest("[hidden]"));
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function recentSummary(assessment) {
    const facts = assessmentFacts(assessment);
    return {
      id: facts.id,
      barangay: facts.barangay,
      latitude: facts.latitude,
      longitude: facts.longitude,
      score: facts.score,
      category: facts.category,
      complete: facts.complete,
      createdAt: facts.createdAt || new Date().toISOString()
    };
  }

  function saveRecent(assessment) {
    const summary = recentSummary(assessment);
    if (!PRIVATE_ASSESSMENT_ID.test(String(summary.id || ""))) return;
    state.recent = [summary, ...state.recent.filter((item) => String(item.id) !== String(summary.id))].slice(0, 10);
    try {
      localStorage.setItem(HISTORY_KEY, JSON.stringify(state.recent));
    } catch {
      // History is optional; assessment remains fully usable when storage is blocked.
    }
  }

  async function loadHistory() {
    let local = [];
    try {
      local = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
      if (!Array.isArray(local)) local = [];
    } catch {
      local = [];
    }
    const seen = new Set();
    state.recent = local.filter((item) => {
      if (!PRIVATE_ASSESSMENT_ID.test(String(item.id || "")) || seen.has(String(item.id))) return false;
      seen.add(String(item.id));
      return true;
    }).slice(0, 10);
    try {
      localStorage.setItem(HISTORY_KEY, JSON.stringify(state.recent));
    } catch {
      // Private history remains optional when browser storage is unavailable.
    }
    renderHistory();
  }

  function renderHistory() {
    if (!state.recent.length) {
      els["history-list"].innerHTML = `<p class="muted">No previously generated scores are available.</p>`;
      return;
    }
    els["history-list"].innerHTML = state.recent.map((item) => `
      <button class="history-item" type="button" data-assessment-id="${escapeHtml(item.id)}">
        <strong>${escapeHtml(item.barangay)}</strong>
        <small>${escapeHtml(formatDate(item.createdAt))} · ${escapeHtml(item.category)}</small>
        <span class="history-score">${item.complete && item.score !== null ? escapeHtml(Math.round(item.score)) : "—"}</span>
      </button>`).join("");
  }

  async function openHistoryAssessment(id) {
    state.liveRequestSequence += 1;
    state.liveHazardResponse = null;
    state.liveHazards = new Map();
    state.liveHazardCoordinates = null;
    renderPointHazardStatus();
    setAssessmentStatus("info", "Loading");
    els["results-empty"].hidden = true;
    els["results-content"].hidden = true;
    els["results-loading"].hidden = false;
    try {
      const payload = await apiFetch(`/assessments/${encodeURIComponent(id)}`);
      const assessment = await hydrateAssessment(payload);
      const facts = assessmentFacts(assessment);
      if (toNumber(facts.latitude) !== null && toNumber(facts.longitude) !== null) {
        state.selection = {
          latitude: Number(facts.latitude),
          longitude: Number(facts.longitude),
          inside: true,
          barangay: { name: facts.barangay },
          source: "score history"
        };
        if (state.map) {
          if (!state.marker) state.marker = L.marker([facts.latitude, facts.longitude], { icon: markerIcon() }).addTo(state.map);
          else state.marker.setLatLng([facts.latitude, facts.longitude]);
          state.map.panTo([facts.latitude, facts.longitude]);
        }
        renderSelection();
      }
      renderAssessment(assessment);
    } catch (error) {
      els["results-loading"].hidden = true;
      els["results-empty"].hidden = false;
      setAssessmentStatus("danger", "Unavailable");
      showToast(`Could not reload score: ${error.message}`, "error");
    }
  }

  function showReportPreview() {
    if (!state.assessment) return;
    const facts = assessmentFacts(state.assessment);
    els["report-preview"].innerHTML = `
      <article class="report-sheet">
        <header class="report-brand">
          <h2>Basafe scoring report</h2>
          <p>Explainable vulnerability screening · Basey, Samar</p>
          <div class="report-meta">Score record ${escapeHtml(textValue(facts.id, "not yet stored"))} · ${escapeHtml(formatDate(facts.createdAt || new Date()))} · Model ${escapeHtml(facts.modelVersion)}</div>
        </header>
        ${buildResultHtml(state.assessment)}
      </article>`;
    if (typeof els["report-dialog"].showModal === "function") {
      els["report-dialog"].showModal();
    } else {
      els["report-dialog"].setAttribute("open", "");
    }
  }

  function filenameFromHeader(header, fallback) {
    if (!header) return fallback;
    const utf = header.match(/filename\*=UTF-8''([^;]+)/i);
    if (utf) return decodeURIComponent(utf[1].replaceAll('"', ""));
    const plain = header.match(/filename="?([^";]+)"?/i);
    return plain ? plain[1] : fallback;
  }

  async function downloadReport() {
    if (!state.assessment) return;
    const facts = assessmentFacts(state.assessment);
    if (!facts.id) {
      showToast("This score has no stored identifier, so a generated PDF is not available.", "error");
      return;
    }
    if (facts.integrityMismatch) {
      showToast("PDF download is blocked because the saved result conflicts with the required missing-hazard rule.", "error");
      return;
    }
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch(`${API_BASE}/assessments/${encodeURIComponent(facts.id)}/report`, {
        headers: { Accept: "application/pdf, application/json" },
        signal: controller.signal
      });
      if (!response.ok) throw new Error(`Report request failed (${response.status})`);
      const contentType = response.headers.get("content-type") || "";
      if (contentType.includes("json")) {
        const payload = unwrap(await response.json()) || {};
        const reportUrl = firstDefined(payload.url, payload.download_url, payload.report_url);
        if (!reportUrl) throw new Error("The report service did not return a PDF or download URL");
        const anchor = document.createElement("a");
        anchor.href = reportUrl;
        anchor.download = "";
        anchor.rel = "noopener";
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
      } else {
        const blob = await response.blob();
        const objectUrl = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = objectUrl;
        anchor.download = filenameFromHeader(
          response.headers.get("content-disposition"),
          `Basafe-score-${facts.id}.pdf`
        );
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        window.setTimeout(() => URL.revokeObjectURL(objectUrl), 2000);
      }
      setApiStatus("online", "Data service available");
    } catch (error) {
      showToast(`PDF report could not be downloaded: ${error.message}`, "error");
    } finally {
      window.clearTimeout(timeout);
    }
  }

  function syncRoutingControls() {
    const ready = Boolean(state.routingStatus?.routing_available);
    const selected = state.selection?.inside === true && !state.selection?.checking;
    const disabled = !ready || !selected || state.routeRequestRunning;
    const mapRouteButton = els["map-evacuation-route"];
    if (mapRouteButton) {
      const hasRoute = state.routeLayers.has("evacuation");
      mapRouteButton.disabled = !ready || state.routeRequestRunning;
      mapRouteButton.classList.toggle("is-ready", ready && selected && !hasRoute);
      mapRouteButton.classList.toggle("has-route", hasRoute);
      mapRouteButton.setAttribute("aria-pressed", String(hasRoute));
      mapRouteButton.title = !ready
        ? "Evacuation routing is unavailable"
        : selected
          ? (hasRoute ? "Recalculate evacuation route" : "Find evacuation route")
          : "Select a location to find an evacuation route";
    }
    if (!els["routing-controls"]) return;
    els["routing-controls"].hidden = !ready;
    els["find-evacuation-route"].disabled = disabled;
    if (!ready) return;
    els["routing-status"].className = "routing-status";
    els["routing-status"].textContent = selected
      ? "This point can be checked against the loaded routing study area."
      : "Select a point inside Basey to check whether detailed evacuation routing is available.";
  }

  async function loadRoutingStatus() {
    const payload = await optionalFetch("/routing/status");
    state.routingStatus = payload || {
      routing_available: false,
      notices: ["Routing status could not be loaded."]
    };
    const ready = Boolean(state.routingStatus.routing_available);
    els["routing-badge"].className = `badge ${ready ? "success" : "neutral"}`;
    els["routing-badge"].textContent = ready ? "Available" : "Data required";
    if (!ready) {
      const notices = normalizeStringList(state.routingStatus.notices);
      els["routing-status"].className = "routing-status is-unavailable";
      els["routing-status"].innerHTML = `
        <strong>Town-proper routing is not yet available</strong>
        <p>${escapeHtml(notices[0] || "Verified routing datasets are required.")}</p>
        <details><summary>Required local data</summary><ul>${notices.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></details>`;
    }
    syncRoutingControls();
  }

  function clearRouteLayers({ clearResult = true } = {}) {
    for (const layer of state.routeLayers.values()) {
      if (state.map?.hasLayer(layer)) state.map.removeLayer(layer);
    }
    state.routeLayers.clear();
    if (state.routeDestinationMarker && state.map?.hasLayer(state.routeDestinationMarker)) {
      state.map.removeLayer(state.routeDestinationMarker);
    }
    state.routeDestinationMarker = null;
    els["map-evacuation-route"]?.classList.remove("has-route");
    els["map-evacuation-route"]?.setAttribute("aria-pressed", "false");
    if (els["route-layer-controls"]) els["route-layer-controls"].hidden = true;
    if (els["map-route-summary"]) els["map-route-summary"].hidden = true;
    if (clearResult && els["routing-result"]) {
      els["routing-result"].hidden = true;
      els["routing-result"].innerHTML = "";
    }
    renderMobileSelectionSummary();
  }

  function addRouteLayer(key, route) {
    if (!state.map || !route?.route) return null;
    const halo = L.geoJSON(route.route, {
      pane: "routePane",
      style: {
        color: "#062b36",
        weight: 14,
        opacity: .9,
        lineCap: "round",
        lineJoin: "round",
        interactive: false
      }
    });
    const routeLine = L.geoJSON(route.route, {
      pane: "routePane",
      style: {
        color: "#55f2d3",
        weight: 8,
        opacity: 1,
        lineCap: "round",
        lineJoin: "round"
      }
    });
    routeLine.bindTooltip("Evacuation route", { sticky: true, className: "route-tooltip" });
    const layer = L.featureGroup([halo, routeLine]).addTo(state.map);
    state.routeLayers.set(key, layer);
    return layer;
  }

  function routeMetricCell(route, field, suffix = "") {
    const value = route?.routing?.[field];
    return value === null || value === undefined ? "Unknown" : `${formatValue(value, 1)}${suffix}`;
  }

  function evacuationCenterPreview(destination) {
    const metadata = destination?.source_metadata && typeof destination.source_metadata === "object"
      ? destination.source_metadata
      : {};
    const photoUrl = safeSourceUrl(firstDefined(destination?.photo_url, metadata.photo_url));
    const photoSourceUrl = safeSourceUrl(firstDefined(destination?.photo_source_url, metadata.photo_source_url));
    const name = textValue(destination?.name, "Evacuation center");
    const barangay = textValue(destination?.barangay, "Barangay not reported");
    const photoAlt = textValue(
      firstDefined(destination?.photo_alt, metadata.photo_alt),
      `Photograph of ${name}`
    );
    const photoSource = textValue(
      firstDefined(destination?.photo_source, metadata.photo_source),
      "Photo source not reported"
    );
    const media = photoUrl
      ? `<figure class="evacuation-preview-media">
          <img src="${escapeHtml(photoUrl)}" alt="${escapeHtml(photoAlt)}" loading="lazy" decoding="async">
          <figcaption>${photoSourceUrl
            ? `<a href="${escapeHtml(photoSourceUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(photoSource)}</a>`
            : escapeHtml(photoSource)}</figcaption>
        </figure>`
      : `<div class="evacuation-preview-placeholder" aria-label="Facility photo not yet supplied">
          <svg viewBox="0 0 48 48" aria-hidden="true"><path d="M7 14h9l3-4h10l3 4h9v25H7V14Z"/><circle cx="24" cy="26" r="8"/><path d="m10 36 8-7 6 5 5-4 9 6"/></svg>
          <span>Facility photo not yet supplied</span>
        </div>`;
    return `<article class="evacuation-preview-card">
      ${media}
      <div class="evacuation-preview-copy">
        <span class="evacuation-preview-kicker">Designated Evacuation Center</span>
        <strong>${escapeHtml(name)}</strong>
        <small>${escapeHtml(barangay)}</small>
      </div>
    </article>`;
  }

  function renderRouteResult(payload) {
    clearRouteLayers({ clearResult: false });
    const selected = payload.routes?.shortest || {
      destination: payload.destination,
      routing: payload.routing,
      route: payload.route
    };
    addRouteLayer("evacuation", selected);
    if (state.map && selected?.destination) {
      const centerIcon = L.divIcon({
        className: "route-destination-marker",
        html: `<span class="evacuation-marker-pin" aria-hidden="true">
          <svg viewBox="0 0 24 24"><path d="m3 11.5 9-7.5 9 7.5M5.5 10v10h13V10"/><circle cx="9.5" cy="13.5" r="1.25"/><circle cx="14.5" cy="13.5" r="1.25"/><path d="M7.5 19v-1.4c0-1.5.8-2.5 2-2.5s2 1 2 2.5V19m1-1.4c0-1.5.8-2.5 2-2.5s2 1 2 2.5V19"/></svg>
        </span>`,
        iconSize: [52, 52],
        iconAnchor: [26, 49],
        popupAnchor: [0, -45],
        tooltipAnchor: [0, -43]
      });
      const preview = evacuationCenterPreview(selected.destination);
      state.routeDestinationMarker = L.marker(
        [selected.destination.latitude, selected.destination.longitude],
        { icon: centerIcon, title: `${selected.destination.name} — designated evacuation center`, keyboard: true }
      ).addTo(state.map)
        .bindTooltip(preview, {
          direction: "top",
          opacity: 1,
          className: "evacuation-center-preview"
        })
        .bindPopup(preview, {
          className: "evacuation-center-popup",
          maxWidth: 300
        })
        .on("popupopen", function () {
          this.closeTooltip();
        });
    }
    const bounds = [];
    for (const layer of state.routeLayers.values()) {
      const layerBounds = layer.getBounds?.();
      if (layerBounds?.isValid()) bounds.push(layerBounds);
    }
    if (bounds.length && state.map) {
      const combined = bounds.reduce((current, item) => current.extend(item), bounds[0]);
      state.map.fitBounds(combined.pad(.18), { padding: [54, 54], maxZoom: 17 });
    }
    const warnings = normalizeStringList(payload.warnings);
    const distanceLabel = routeMetricCell(selected, "distance_m", " m");
    const timeLabel = routeMetricCell(selected, "estimated_walk_minutes", " min");
    els["routing-result"].innerHTML = `
      <p class="eyebrow">Nearest reachable destination</p>
      <h3>${escapeHtml(selected.destination.name)}</h3>
      <p class="route-designation">Designated evacuation center</p>
      <div class="route-primary-metrics">
        <div><strong>${distanceLabel}</strong><span>Walking distance</span></div>
        <div><strong>About ${timeLabel}</strong><span>Estimated time</span></div>
      </div>
      <p class="route-mode-label"><i class="route-inline-line" aria-hidden="true"></i> Route highlighted on the map</p>
      ${warnings.length ? `<details class="route-notices"><summary>Data notice</summary>${warnings.map((item) => `<p class="route-warning">${escapeHtml(item)}</p>`).join("")}</details>` : ""}
      <details class="route-technical"><summary>Route details</summary>
        <dl><dt>Algorithm</dt><dd>${escapeHtml(payload.provenance?.algorithm || "astar")}</dd>
        <dt>Road data</dt><dd>${escapeHtml(payload.provenance?.road_graph_version || "Not reported")}</dd>
        <dt>Center source</dt><dd>${escapeHtml(selected.destination.source_name || "Basey MDRRMO")}</dd></dl>
      </details>`;
    els["routing-result"].hidden = false;
    els["route-layer-controls"].hidden = state.routeLayers.size === 0;
    els["toggle-evacuation-route"].checked = state.routeLayers.has("evacuation");
    els["map-route-destination"].textContent = selected.destination.name;
    els["map-route-distance"].textContent = `${distanceLabel} · ${timeLabel}`;
    els["map-route-summary"].hidden = false;
    renderMobileSelectionSummary();
  }

  async function requestRoute() {
    const selection = state.selection;
    if (!selection || selection.inside !== true) {
      showToast("Select a location inside Basey before finding an evacuation route.");
      return;
    }
    if (state.routeRequestRunning) return;
    state.routeRequestRunning = true;
    syncRoutingControls();
    els["routing-panel"].open = true;
    els["routing-result"].hidden = false;
    els["routing-result"].innerHTML = '<p class="loading-line"><span class="spinner" aria-hidden="true"></span> Finding the nearest reachable designated center…</p>';
    try {
      const payload = await apiFetch("/route", {
        method: "POST",
        timeout: 20000,
        body: JSON.stringify({
          latitude: selection.latitude,
          longitude: selection.longitude,
          mode: "shortest",
          scenario: "multi_hazard",
          include_comparison: false
        })
      });
      renderRouteResult(payload);
    } catch (error) {
      clearRouteLayers({ clearResult: false });
      const messages = {
        outside_routing_area: "Detailed evacuation routing is currently limited to the loaded Basey routing study area.",
        routing_graph_missing: "The local pedestrian road graph is not available.",
        no_evacuation_centers: "No verified designated evacuation centers are loaded.",
        no_reachable_center: "No designated evacuation center is reachable on the loaded walking graph.",
        incomplete_hazard_data: "Mapped hazard information is incomplete on the required road segments."
      };
      els["routing-result"].innerHTML = `
        <div class="route-error"><strong>Route unavailable</strong><p>${escapeHtml(messages[error.code] || error.message)}</p></div>`;
      showToast(messages[error.code] || "The route could not be generated.", "error");
    } finally {
      state.routeRequestRunning = false;
      syncRoutingControls();
    }
  }

  function bindEvents() {
    let searchDebounce = null;
    els["search-form"].addEventListener("submit", (event) => {
      event.preventDefault();
      const query = els["location-search"].value.trim();
      if (query.length < 2) {
        showToast("Enter at least two characters to search.", "error");
        return;
      }
      searchLocation(query);
    });

    els["search-results"].addEventListener("click", (event) => {
      const button = event.target.closest("[data-search-index]");
      if (!button) return;
      const result = els["search-results"]._results?.[Number(button.dataset.searchIndex)];
      chooseSearchResult(result);
    });

    els["location-search"].addEventListener("input", () => {
      window.clearTimeout(searchDebounce);
      const query = els["location-search"].value.trim();
      if (query.length < 2) {
        state.searchRequestSequence += 1;
        els["search-results"].innerHTML = "";
        els["location-search"].setAttribute("aria-expanded", "false");
        return;
      }
      searchDebounce = window.setTimeout(() => searchLocation(query), 300);
    });
    els["location-search"].addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown") {
        const first = els["search-results"].querySelector("[data-search-index]");
        if (first) { event.preventDefault(); first.focus(); }
      } else if (event.key === "Escape") {
        state.searchRequestSequence += 1;
        els["search-results"].innerHTML = "";
        els["location-search"].setAttribute("aria-expanded", "false");
        clearSearchHighlight();
      }
    });
    els["search-results"].addEventListener("keydown", (event) => {
      const options = [...els["search-results"].querySelectorAll("[data-search-index]")];
      const index = options.indexOf(event.target.closest("[data-search-index]"));
      if (index < 0) return;
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        options[(index + (event.key === "ArrowDown" ? 1 : -1) + options.length) % options.length].focus();
      } else if (event.key === "Enter") {
        event.preventDefault();
        options[index].click();
      } else if (event.key === "Escape") {
        event.preventDefault();
        els["search-results"].innerHTML = "";
        els["location-search"].setAttribute("aria-expanded", "false");
        els["location-search"].focus();
      }
    });

    els["coordinate-form"].addEventListener("submit", (event) => {
      event.preventDefault();
      selectLocation(els.latitude.value, els.longitude.value, "coordinate input");
      els["coordinate-form"].closest("details")?.removeAttribute("open");
    });

    els["run-assessment"].addEventListener("click", runAssessment);
    els["mobile-assess"]?.addEventListener("click", runAssessment);
    els["mobile-view-map"]?.addEventListener("click", () => showMobileWorkspaceView("map"));
    els["mobile-view-score"]?.addEventListener("click", () => showMobileWorkspaceView("score"));
    els["empty-primary-action"]?.addEventListener("click", () => {
      if (els["empty-primary-action"].dataset.action === "score") runAssessment();
      else {
        clearSelection();
        if (state.map) state.map.fitBounds(BASEY_FALLBACK_BOUNDS);
        els["location-search"]?.focus();
      }
    });
    els["mobile-selection-details"]?.addEventListener("click", () => setControlsOpen(true));
    els["selection-summary"]?.addEventListener("click", (event) => {
      if (!event.target.closest("[data-return-basey]")) return;
      clearSelection();
      if (state.map) state.map.fitBounds(BASEY_FALLBACK_BOUNDS);
      els["location-search"]?.focus();
    });
    els["clear-selection"].addEventListener("click", clearSelection);
    els["open-controls"]?.addEventListener("click", () => setControlsOpen(true));
    els["close-controls"]?.addEventListener("click", () => setControlsOpen(false));
    els["controls-backdrop"]?.addEventListener("click", () => setControlsOpen(false));
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && document.body.classList.contains("controls-open")) {
        event.preventDefault();
        setControlsOpen(false);
        return;
      }
      keepFocusInsideControls(event);
    });
    window.addEventListener("resize", syncControlsAccessibility);
    els["reset-map"]?.addEventListener("click", () => {
      clearSelection();
      if (state.map) state.map.fitBounds(BASEY_FALLBACK_BOUNDS);
    });
    els["start-new-assessment"]?.addEventListener("click", () => {
      clearSelection();
      setControlsOpen(false);
      els["location-search"]?.focus();
    });
    els["use-location"]?.addEventListener("click", () => {
      if (!navigator.geolocation) {
        showToast("Current location is not supported by this browser. Use search, coordinates, or the map instead.", "error");
        return;
      }
      els["use-location"].disabled = true;
      const locationLabel = els["use-location"].querySelector("span");
      if (locationLabel) locationLabel.textContent = "Locating…";
      els["use-location"].setAttribute("aria-label", "Locating current position");
      navigator.geolocation.getCurrentPosition(
        (position) => {
          els["use-location"].disabled = false;
          if (locationLabel) locationLabel.textContent = "My location";
          els["use-location"].setAttribute("aria-label", "Use current location");
          selectLocation(position.coords.latitude, position.coords.longitude, "current_location", "Current location");
        },
        (error) => {
          els["use-location"].disabled = false;
          if (locationLabel) locationLabel.textContent = "My location";
          els["use-location"].setAttribute("aria-label", "Use current location");
          showToast(error.code === 1 ? "Location permission was not granted. You can still search, enter coordinates, or click the map." : "Current location could not be retrieved. Try again or select the point manually.", "error");
        },
        { enableHighAccuracy: true, timeout: 12000, maximumAge: 60000 }
      );
    });

    els["fit-basey"].addEventListener("click", () => {
      if (!state.map) return;
      if (state.boundaryLayer?.getBounds().isValid()) state.map.fitBounds(state.boundaryLayer.getBounds().pad(.04));
      else state.map.fitBounds(BASEY_FALLBACK_BOUNDS);
    });

    els["toggle-boundary"].addEventListener("change", (event) => {
      if (!state.map || !state.boundaryLayer) return;
      if (event.target.checked) state.boundaryLayer.addTo(state.map);
      else state.map.removeLayer(state.boundaryLayer);
    });

    els["toggle-barangays"].addEventListener("change", (event) => {
      if (!state.map || !state.barangayLayer) return;
      if (event.target.checked) state.barangayLayer.addTo(state.map);
      else state.map.removeLayer(state.barangayLayer);
    });

    els["basemap-controls"].addEventListener("change", (event) => {
      const input = event.target.closest('input[name="basemap"]');
      if (input?.checked) setBasemap(input.value);
    });

    els["hazard-layer-controls"].addEventListener("change", (event) => {
      const checkbox = event.target.closest("[data-service-index]");
      if (!checkbox) return;
      const dataset = state.ulapServices[Number(checkbox.dataset.serviceIndex)];
      if (dataset) toggleHazardLayer(dataset, checkbox);
    });
    els["hazard-layer-controls"].addEventListener("input", (event) => {
      const range = event.target.closest("[data-layer-opacity-index]");
      if (!range) return;
      const dataset = state.ulapServices[Number(range.dataset.layerOpacityIndex)];
      const entry = dataset ? state.hazardLayers.get(hazardLayerStateKey(dataset)) : null;
      const opacity = Number(range.value) / 100;
      range.nextElementSibling.textContent = `${range.value}%`;
      entry?.layer?.setStyle?.({ fillOpacity: opacity, opacity: Math.min(1, opacity + .35) });
      entry?.layer?.setOpacity?.(opacity);
    });

    els["history-list"].addEventListener("click", (event) => {
      const button = event.target.closest("[data-assessment-id]");
      if (button) {
        setControlsOpen(false, { restoreFocus: false });
        openHistoryAssessment(button.dataset.assessmentId);
      }
    });

    els["find-evacuation-route"]?.addEventListener("click", requestRoute);
    els["map-evacuation-route"]?.addEventListener("click", requestRoute);
    els["clear-route"]?.addEventListener("click", () => clearRouteLayers());
    els["map-route-clear"]?.addEventListener("click", () => clearRouteLayers());
    els["toggle-evacuation-route"]?.addEventListener("change", (event) => {
      const layer = state.routeLayers.get("evacuation");
      if (!layer || !state.map) return;
      if (event.target.checked) layer.addTo(state.map);
      else state.map.removeLayer(layer);
    });

    els["preview-report"].addEventListener("click", showReportPreview);
    els["close-report"].addEventListener("click", () => els["report-dialog"].close());
    els["report-dialog"].addEventListener("click", (event) => {
      if (event.target === els["report-dialog"]) els["report-dialog"].close();
    });
    els["print-preview"].addEventListener("click", () => window.print());
    els["download-report"].addEventListener("click", downloadReport);
    els["download-report-dialog"].addEventListener("click", downloadReport);
  }

  async function start() {
    cacheElements();
    bindEvents();
    syncControlsAccessibility();
    syncMobileAssessmentAction();
    syncSelectionExperience();
    initMap();
    await Promise.all([loadSpatialData(), loadHistory(), loadRoutingStatus()]);
    const routeAssessment = location.pathname.match(/^\/assessment\/([A-Za-z0-9_-]{20,128})\/?$/);
    if (routeAssessment) await openHistoryAssessment(routeAssessment[1]);
  }

  start().catch((error) => {
    els["map-loading"].hidden = true;
    setApiStatus("offline", "Initialization incomplete");
    showToast(`Basafe could not finish loading: ${error.message}`, "error");
  });
})();
