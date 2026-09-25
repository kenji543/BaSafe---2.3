(() => {
  "use strict";

  const pages = {
    "/limitations": {
      eyebrow: "Scope and interpretation",
      title: "Limitations",
      summary: "Use Basafe as a transparent first screening step—not as a final decision, warning service, or declaration of safety.",
      sections: [
        ["What the system covers", "Basafe combines selected flood, liquefaction, and ground-shaking evidence for locations within Basey, Samar. A complete score is available only when every required verified input is present."],
        ["What the system does not do", "It does not forecast disasters, monitor emergencies in real time, inspect a site, evaluate a structure, certify a property, approve land use, issue a permit recommendation, or declare a location safe or unsafe."],
        ["Data limitations", "Every result depends on the date, scale, coverage, classification, availability, and permitted use of its cited sources. A missing feature or unavailable service is not interpreted as Low."],
        ["Model limitations", "The fuzzy configuration remains a demonstration research model until qualified hazard, planning, geotechnical, and disaster-risk specialists validate its transformations, weights, rules, membership functions, and thresholds."],
        ["Required disclaimer", "This report is a preliminary multi-hazard screening output based on selected available data. It does not certify that a location is safe or unsafe and does not replace official hazard, planning, engineering, geological, geotechnical, or regulatory assessment."]
      ]
    },
    "/about": {
      eyebrow: "Research project",
      title: "About Basafe",
      summary: "A public Web-GIS research prototype that makes selected hazard evidence, model reasoning, uncertainty, and limitations easier to understand.",
      sections: [
        ["Purpose", "Basafe explores how verified government hazard classifications and Basey geographic context can be organized into a reproducible, plain-language preliminary screening workflow."],
        ["Public access", "Residents, property researchers, students, community organizations, visitors, businesses, planners, and government personnel use the same public functions. Core mapping and scoring features require no account."],
        ["Institutional boundaries", "GeoRiskPH and mandated government agencies remain authoritative hazard sources. Basey municipal offices are not represented as system administrators, validators, adopters, approving authorities, or official recipients."],
        ["Research team", "Developed as an Information Technology capstone research project at Eastern Visayas State University. Project contact information will be published here when approved for public release."]
      ]
    },
    "/privacy": {
      eyebrow: "Data handling",
      title: "Privacy",
      summary: "Basafe minimizes personal data and keeps public scoring functions available without registration.",
      sections: [
        ["Private score access", "The current prototype does not require an account. Selected coordinates, source responses, model explanations, and generated-report metadata are stored for reproducibility, but access uses an unguessable score token retained by this device. The service does not publish a shared score history."],
        ["Location permission", "Current-location access is requested only after the user activates the feature. The browser supplies coordinates after permission; Basafe does not continuously track the device."],
        ["Local and offline data", "The service worker stores versioned application files and may retain permitted previously viewed responses. Cached information is not presented as current without its retrieval and expiration status."],
        ["Historical records", "Personally identifiable incident data must not be published. Local CLUP, CDRA, and historical information is displayed only when permitted and appropriately prepared."],
        ["Damage reports", "The Report Damage form collects the pinned location, address details, the type and severity of damage, an optional description and photos, and your name and phone number, only after you agree. They are used by Basey MDRRMO responders to find the location and contact you about that report, and are never shown publicly or shared with other residents. Photos are resized and their location and device details are removed before storage. Reports are kept only as long as responders need them for response and recovery, then deleted."],
        ["Optional accounts", "Accounts are not implemented, and reporting damage does not need one. If accounts are added later, they may support personal history and saved locations only; public scoring will remain available without sign-in."]
      ]
    },
    "/offline": {
      eyebrow: "Connection status",
      title: "Offline information",
      summary: "The application shell can remain available, but new hazard data and complete scores normally require current network access.",
      sections: [
        ["Available offline", "Previously loaded informational pages and the application shell may remain available. Previously generated reports may be reopened only when stored locally and permitted by source policy."],
        ["Requires the Basafe service", "Location searches and new scoring requests require access to the Basafe backend. Scoring uses available local hazard data; upstream source availability is reported separately and matters when maintainers synchronize or replace that data."],
        ["Stale information", "Cached source data retains retrieval and expiration metadata. Basafe never implies that cached information is current and never converts missing required inputs into a complete result."],
        ["Next step", "Reconnect and calculate the score again when current source access is restored."]
      ]
    }
  };

  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[character]));
  const title = document.getElementById("page-title");
  const eyebrow = document.getElementById("page-eyebrow");
  const summary = document.getElementById("page-summary");
  const sections = document.getElementById("page-sections");

  function renderPage(page) {
    document.title = `${page.title} | Basafe`;
    title.textContent = page.title;
    eyebrow.textContent = page.eyebrow;
    summary.textContent = page.summary;
    sections.innerHTML = page.sections.map(([heading, body]) => `<section class="info-card"><h2>${escapeHtml(heading)}</h2><p>${escapeHtml(body)}</p></section>`).join("");
  }

  async function renderSources() {
    const page = { eyebrow: "Source transparency", title: "Data Sources", summary: "Review the organizations, layers, source details, and limitations attached to the evidence used by Basafe." };
    document.title = "Data Sources | Basafe";
    title.textContent = page.title;
    eyebrow.textContent = page.eyebrow;
    summary.textContent = page.summary;
    sections.innerHTML = `<section class="info-card"><h2>Current configured services</h2><div id="source-live" class="source-live" role="status">Loading source details…</div></section><section class="info-card"><h2>Supporting Basey data</h2><p>Municipal and barangay boundaries, permitted CLUP and CDRA references, appropriately prepared historical disaster records, and other approved local context may be displayed with provenance. Their inclusion does not imply LGU administration or endorsement.</p></section><section class="info-card"><h2>Source policy</h2><p>Basafe does not create fake service URLs, classifications, or agency endorsements. Missing, invalid, out-of-coverage, expired, or unreachable data remains explicit and can block a complete score.</p></section>`;
    const live = document.getElementById("source-live");
    try {
      const response = await fetch("/api/v1/ulap/services", { headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error("Source service unavailable");
      const payload = await response.json();
      const items = payload.items || payload.services || payload;
      const values = Array.isArray(items) ? items : Object.values(items || {});
      live.innerHTML = values.map((item) => {
        const label = item.display_name || item.expected_layer_name || item.name || item.key || item.slug || "Configured source";
        const organization = item.agency || item.organization || item.provider || "Organization not reported";
        const validation = item.runtime_validation || {};
        const checked = validation.checked_at || item.checked_at || item.retrieved_at || "Not reported";
        const metadata = validation.metadata || {};
        const layer = item.expected_layer_name || metadata.name || item.layer_id || "Not reported";
        const sourceDate = item.source_date || metadata.source_date || metadata.data_date || item.verification?.verified_on || "Not reported";
        const retrieved = validation.retrieved_at || metadata.retrieved_at || checked;
        const description = metadata.description || item.attribution || item.verification?.reason || "No additional source description was reported.";
        const sourceUrl = item.layer_url || item.service_url || "";
        return `<article><div class="source-card-head"><strong>${escapeHtml(label)}</strong></div><dl class="source-details"><dt>Organization</dt><dd>${escapeHtml(organization)}</dd><dt>Layer</dt><dd>${escapeHtml(layer)}</dd><dt>Source date</dt><dd>${escapeHtml(sourceDate)}</dd><dt>Last retrieval/check</dt><dd>${escapeHtml(retrieved)}</dd>${sourceUrl ? `<dt>Service</dt><dd><a href="${escapeHtml(sourceUrl)}" rel="noreferrer">View configured ArcGIS layer</a></dd>` : ""}</dl><p class="source-description">${escapeHtml(description)}</p></article>`;
      }).join("") || "No source metadata is configured.";
    } catch (error) {
      live.textContent = "Source details could not be loaded. Reconnect and try again; unavailable data must not be interpreted as Low.";
    }
  }

  const path = location.pathname.replace(/\/$/, "") || "/";
  if (path === "/data-sources") renderSources();
  else renderPage(pages[path] || pages["/offline"]);

  const menuButton = document.querySelector(".menu-toggle");
  const navigation = document.querySelector(".site-nav");
  menuButton?.addEventListener("click", () => {
    const open = menuButton.getAttribute("aria-expanded") !== "true";
    menuButton.setAttribute("aria-expanded", String(open));
    navigation?.classList.toggle("is-open", open);
  });
})();
