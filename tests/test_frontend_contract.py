from __future__ import annotations

import re
import json
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = PROJECT_ROOT / "web"


class IdCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.password_inputs = 0
        self.forms: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        if attributes.get("id"):
            self.ids.append(str(attributes["id"]))
        if tag == "input" and attributes.get("type", "").lower() == "password":
            self.password_inputs += 1
        if tag == "form":
            self.forms.append(attributes.get("id") or "")


class FrontendContractTests(unittest.TestCase):
    def test_vercel_bundle_includes_routing_graph(self) -> None:
        ignore_rules = (PROJECT_ROOT / ".vercelignore").read_text(encoding="utf-8")
        deployment = json.loads(
            (PROJECT_ROOT / "vercel.json").read_text(encoding="utf-8")
        )
        function_settings = deployment["functions"]["api/index.py"]
        self.assertIn("!data/routing/**", ignore_rules)
        self.assertIn("data/routing/**", function_settings["includeFiles"])

    def test_only_approved_pages_are_present(self) -> None:
        html_pages = {path.name for path in WEB_ROOT.glob("*.html")}
        self.assertEqual(
            html_pages,
            {"index.html", "map.html", "methodology.html", "info.html", "report-damage.html"},
        )

    def test_damage_report_page_is_submit_only_with_consent(self) -> None:
        page = (WEB_ROOT / "report-damage.html").read_text(encoding="utf-8")
        script = (WEB_ROOT / "report-damage.js").read_text(encoding="utf-8")
        self.assertIn('name="consent" type="checkbox" value="yes" required', page)
        self.assertIn("not an emergency hotline", page)
        self.assertIn("/location/identify", script)
        self.assertIn('"/api/v1/reports"', script)
        self.assertNotIn("/admin", script)
        for page_name in ("map.html", "index.html"):
            self.assertIn('href="/report-damage"', (WEB_ROOT / page_name).read_text(encoding="utf-8"))

    def test_main_page_contains_required_unified_workflow_controls(self) -> None:
        parser = IdCollector()
        parser.feed((WEB_ROOT / "map.html").read_text(encoding="utf-8"))
        self.assertEqual(len(parser.ids), len(set(parser.ids)), "Duplicate HTML IDs")
        self.assertEqual(parser.password_inputs, 0)
        required_ids = {
            "map",
            "search-form",
            "coordinate-form",
            "latitude",
            "longitude",
            "hazard-layer-controls",
            "basemap-controls",
            "basemap-attribution",
            "map-legend",
            "run-assessment",
            "results-content",
            "preview-report",
            "download-report",
            "history-list",
            "use-location",
            "reset-map",
            "mobile-assess",
            "open-controls",
        }
        self.assertTrue(required_ids.issubset(parser.ids))

    def test_landing_page_contains_required_public_sections(self) -> None:
        page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
        self.assertGreaterEqual(page.count("Score a Location"), 3)
        for phrase in (
            "Understand the hazards affecting a location.",
            "How it works",
            "Hazards included",
            "How the score works",
            "Why the result is explainable",
            "Who this is for",
            "Data sources",
            "Limitations",
            "Frequently asked questions",
        ):
            self.assertIn(phrase, page)

    def test_frontend_calls_only_approved_api_groups(self) -> None:
        scripts = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (WEB_ROOT / "app.js", WEB_ROOT / "methodology.js")
        )
        required_paths = {
            "/boundary",
            "/barangays",
            "/ulap/status",
            "/ulap/services",
            "/hazards/at-location",
            "/location/search",
            "/location/identify",
            "/assessments",
            "/incidents/nearby",
            "/clup-references/by-location",
            "/methodology",
            "/data-sources",
        }
        self.assertTrue(all(path in scripts for path in required_paths))
        for forbidden in (
            "/users",
            "/roles",
            "/permissions",
            "/admin",
            "/staff",
            "/audit",
            "/activity",
            "/sessions",
            "/approvals",
            "/datasets/upload",
            "/datasets/publish",
            "/fuzzy-models/editor",
            "/auth/register",
            "/auth/login",
        ):
            self.assertNotIn(forbidden, scripts)

    def test_recent_assessment_history_is_device_local(self) -> None:
        script = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
        history_function = script.split("async function loadHistory()", 1)[1].split(
            "function renderHistory()", 1
        )[0]
        self.assertIn("localStorage.getItem(HISTORY_KEY)", history_function)
        self.assertNotIn('optionalFetch("/assessments")', history_function)
        self.assertIn("This device only", (WEB_ROOT / "map.html").read_text(encoding="utf-8"))

    def test_source_page_renders_provenance_fields_from_api_contract(self) -> None:
        script = (WEB_ROOT / "info.js").read_text(encoding="utf-8")
        for field in (
            "item.agency",
            "item.expected_layer_name",
            "Source date",
            "Last retrieval/check",
            "item.layer_url",
        ):
            self.assertIn(field, script)
        self.assertNotIn("pending_verification", script)
        self.assertNotIn("status-chip", script)

    def test_hidden_attribute_cannot_be_overridden_by_component_layout(self) -> None:
        for stylesheet in ("styles.css", "site.css"):
            css = (WEB_ROOT / stylesheet).read_text(encoding="utf-8")
            self.assertRegex(
                css,
                re.compile(r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important", re.DOTALL),
            )

    def test_basemaps_are_mutually_exclusive_and_hazards_remain_overlays(self) -> None:
        page = (WEB_ROOT / "map.html").read_text(encoding="utf-8")
        script = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
        self.assertEqual(page.count('name="basemap"'), 3)
        for value in ("streets", "satellite", "terrain"):
            self.assertIn(f'value="{value}"', page)
            self.assertIn(f"{value}:", script)
        self.assertIn('value="satellite" checked', page)
        self.assertIn('setBasemap("satellite")', script)
        self.assertIn("function selectLocationFromMapEvent", script)
        self.assertGreaterEqual(script.count('on("click", selectLocationFromMapEvent)'), 4)
        self.assertIn("Barangay matching is optional", page)
        self.assertIn('class="map-search-overlay"', page)
        self.assertIn('placeholder="Street, place, barangay, or coordinates"', page)
        self.assertIn('aria-autocomplete="list"', page)
        self.assertIn("highlightSearchResult", script)
        self.assertIn("Street located. Select a specific point", script)
        self.assertNotIn('class="panel source-health-panel" open', page)
        self.assertNotIn('class="panel layers-panel" open', page)
        self.assertIn("World_Imagery/MapServer/tile/{z}/{y}/{x}", script)
        self.assertIn("World_Topo_Map/MapServer/tile/{z}/{y}/{x}", script)
        self.assertIn("maxNativeZoom: 18", script)
        self.assertIn("maxBoundsViscosity: 1", script)
        self.assertIn("function lockMapToBasey", script)
        self.assertIn("function hazardLayerStateKey", script)
        self.assertIn('pane: "hazardOverlayPane"', script)
        self.assertIn('input[name="basemap"]', script)
        self.assertIn("function arcGisExportOverlay", script)
        self.assertIn('source.hostname !== "ulap-hazards.georisk.gov.ph"', script)
        self.assertIn('displayMode = "arcgis_export"', script)
        self.assertIn('key: "rain_induced_landslide"', script)
        self.assertIn('label: "Rain-induced landslide"', script)
        self.assertIn("Optional map context · not used in scoring", script)
        self.assertIn('renderMode === "arcgis_export"', script)

    def test_disclaimer_and_missing_data_rule_are_visible(self) -> None:
        page_text = " ".join(
            path.read_text(encoding="utf-8")
            for path in (
                WEB_ROOT / "index.html",
                WEB_ROOT / "map.html",
                WEB_ROOT / "methodology.html",
                WEB_ROOT / "app.js",
            )
        )
        self.assertRegex(
            page_text,
            re.compile(r"does not certify.{0,40}safe or unsafe", re.IGNORECASE),
        )
        self.assertRegex(
            page_text,
            re.compile(
                r"missing (?:information|data).{0,80}(?:not|never).{0,30}low",
                re.IGNORECASE | re.DOTALL,
            ),
        )
        self.assertRegex(
            page_text,
            re.compile(r"(?:demonstration|unvalidated).{0,30}model", re.IGNORECASE),
        )

    def test_map_workflow_uses_progressive_disclosure_and_accessible_tabs(self) -> None:
        page = (WEB_ROOT / "map.html").read_text(encoding="utf-8")
        script = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
        styles = (WEB_ROOT / "styles.css").read_text(encoding="utf-8")
        pwa_script = (WEB_ROOT / "pwa.js").read_text(encoding="utf-8")

        for element_id in (
            "mobile-selection-summary",
            "results-empty-title",
            "results-empty-copy",
            "results-empty-steps",
            "empty-primary-action",
        ):
            self.assertIn(f'id="{element_id}"', page)

        self.assertIn("function syncSelectionExperience", script)
        self.assertIn("function buildInterpretationSummary", script)
        self.assertIn("Main mapped condition in this score", script)
        self.assertIn('<details class="hazard-details">', script)
        self.assertIn('<details class="layer-service-details">', script)
        self.assertIn('event.key === "ArrowRight"', script)
        self.assertIn('event.key === "ArrowLeft"', script)
        self.assertIn('event.key === "Home"', script)
        self.assertIn('event.key === "End"', script)
        self.assertIn('tab.setAttribute("tabindex"', script)
        self.assertIn('Unavailable: ${escapeHtml(missing.join(", "))}', script)

        hazard_builder = script.split("function buildHazards", 1)[1].split(
            "function buildMemberships", 1
        )[0]
        self.assertLess(
            hazard_builder.index('<details class="hazard-details">'),
            hazard_builder.index('<div class="model-transform-value">'),
        )

        self.assertIn("body.has-selection .results-panel", styles)
        self.assertIn("body.has-assessment .results-panel", styles)
        self.assertIn(".mobile-selection-summary", styles)
        self.assertIn("data-update-later", pwa_script)
        self.assertIn("notice.remove()", pwa_script)

    def test_map_exposes_one_clear_town_proper_evacuation_route(self) -> None:
        page = (WEB_ROOT / "map.html").read_text(encoding="utf-8")
        script = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
        for element_id in (
            "routing-panel",
            "routing-status",
            "find-evacuation-route",
            "map-evacuation-route",
            "routing-result",
            "route-layer-controls",
            "toggle-evacuation-route",
            "clear-route",
            "map-route-summary",
            "map-route-destination",
            "map-route-distance",
            "map-route-clear",
        ):
            self.assertIn(f'id="{element_id}"', page)
        self.assertIn('id="clear-selection" class="map-tool" type="button" title="Clear selected point" disabled hidden', page)
        self.assertIn('id="reset-map" class="map-tool" type="button" title="Reset map and selection" hidden', page)
        for phrase in (
            "Find evacuation route",
            "Evacuation route",
            "Nearest reachable destination",
            "Designated Evacuation Center",
            "Route highlighted on the map",
        ):
            self.assertIn(phrase, page + script)
        for removed in (
            'id="routing-scenario"',
            'id="find-lower-hazard-route"',
            'id="find-shortest-route"',
            'id="toggle-lower-route"',
            "Lower-Hazard Route",
            "Route comparison",
            "Mapped exposure",
        ):
            self.assertNotIn(removed, page + script)
        self.assertIn('apiFetch("/route"', script)
        self.assertIn('optionalFetch("/routing/status")', script)
        self.assertIn('pane: "routePane"', script)
        self.assertIn("function evacuationCenterPreview", script)
        self.assertIn("Facility photo not yet supplied", script)
        self.assertIn('className: "evacuation-center-preview"', script)
        self.assertIn('className: "evacuation-center-popup"', script)
        styles = (WEB_ROOT / "styles.css").read_text(encoding="utf-8")
        self.assertIn(".evacuation-marker-pin", styles)
        self.assertIn(".evacuation-preview-card", styles)
        routing_text = (page + script).casefold()
        for prohibited in ("safest route", "guaranteed safe route", "safe area"):
            self.assertNotIn(prohibited, routing_text)

    def test_fallback_and_methodology_use_canonical_disclaimer(self) -> None:
        disclaimer = json.loads(
            (PROJECT_ROOT / "config" / "fuzzy_model.json").read_text(
                encoding="utf-8"
            )
        )["disclaimer"]
        data_disclaimer = json.loads(
            (PROJECT_ROOT / "config" / "data_sources.json").read_text(
                encoding="utf-8"
            )
        )["required_disclaimer"]
        self.assertEqual(data_disclaimer, disclaimer)
        self.assertIn(
            f'const DISCLAIMER = "{disclaimer}";',
            (WEB_ROOT / "app.js").read_text(encoding="utf-8"),
        )
        self.assertIn(
            disclaimer,
            (WEB_ROOT / "methodology.html").read_text(encoding="utf-8"),
        )

    def test_javascript_has_valid_syntax_when_node_is_available(self) -> None:
        try:
            for filename in (
                "app.js",
                "methodology.js",
                "landing.js",
                "info.js",
                "pwa.js",
                "service-worker.js",
                "report-damage.js",
            ):
                completed = subprocess.run(
                    ["node", "--check", str(WEB_ROOT / filename)],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    msg=completed.stdout + completed.stderr,
                )
        except FileNotFoundError:
            self.skipTest("Node.js is not installed; browser syntax check skipped.")

    def test_pwa_contract_and_offline_safety(self) -> None:
        manifest = json.loads(
            (WEB_ROOT / "manifest.webmanifest").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["start_url"], "/")
        self.assertTrue(any("maskable" in icon["purpose"] for icon in manifest["icons"]))
        worker = (WEB_ROOT / "service-worker.js").read_text(encoding="utf-8")
        self.assertIn("/offline", worker)
        self.assertIn("url.pathname.startsWith(\"/api/\")", worker)
        self.assertIn("event.respondWith(fetch(request))", worker)
        self.assertIn("shellCache.match(url.pathname)", worker)
        info_script = (WEB_ROOT / "info.js").read_text(encoding="utf-8")
        self.assertIn('pages[path] || pages["/offline"]', info_script)


if __name__ == "__main__":
    unittest.main()
