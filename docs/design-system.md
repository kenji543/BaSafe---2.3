# Design system reference (redesign baseline)

This describes the current UI's structural DNA in generic terms — patterns and
tokens, not the specific content any screen currently shows. It exists as a
baseline for redesign work: everything here lives in the frontend styling
layer (`web/*.css`, `web/admin/admin.css`) and page markup, so restyling it
does not touch `geosafe/`, the API, or the database.

## Visual identity system

- **A dual-palette split by trust context**: a deep, saturated color (used for
  headers, footers, hero backgrounds, and emphasis surfaces) paired with a
  lighter neutral background for scannable content. A single accent hue
  carries all primary actions and interactive affordances, with a second,
  brighter variant of it reserved for hover/active states.
- **A small closed set of semantic colors** for status/severity communication
  (a 5-step spectrum from favorable to concerning), applied consistently as
  left-border accents, dots, and badges rather than full color-fills — keeps
  alarming colors from dominating a page.
- **Category-coding by color+icon pairing**: whenever content splits into a
  fixed small number of distinct types/categories, each gets its own
  consistent hue+icon combination used everywhere that category appears
  (cards, legends, charts, badges) so users learn the mapping once.
- **One display typeface, weight-driven hierarchy**: no serif/sans mixing —
  hierarchy comes from very heavy weights on headings (800–900) with tight
  negative letter-spacing, versus regular/medium body text. Numbers get
  tabular-nums for alignment in data contexts.
- **A restrained geometry scale**: 2–3 border-radius sizes (small/medium/large)
  and 3–4 shadow depths (subtle → pronounced), reused everywhere rather than
  invented per-component — gives a coherent "soft, rounded, layered" feel
  without visual noise.

## Page archetypes

1. **Marketing/landing shell** — sticky translucent (blurred) header, a hero
   with copy on one side and a custom illustrative visualization on the
   other, alternating full-width tinted/untinted content bands, a component
   gallery of feature cards, a metrics/trust strip, an FAQ accordion, and a
   footer with credibility badges. Built to be read top-to-bottom by a
   first-time, possibly skeptical visitor.
2. **Application/tool shell** — abandons page-scroll for a fixed-viewport
   workspace: persistent header, a primary work surface (map/canvas/editor)
   plus a docked side panel for controls and results. Denser type scale,
   fewer animations, optimized for a returning user doing a task repeatedly
   rather than being persuaded.
3. **Focused single-task flow** — a form-like sequence (select → describe →
   confirm) that keeps the marketing shell's chrome for brand continuity but
   strips everything else down to one linear task, with inline validation and
   state-preserving error recovery rather than modal interruptions.
4. **Internal/operational dashboard** — deliberately distinct visual identity
   (different typeface and palette from the public-facing surfaces) so it's
   never mistaken for the public product, plus a persistent environment
   indicator. Standard sidebar-nav + summary-card-grid + alert/queue-list +
   detail-drawer pattern.

## Component patterns

- **Buttons**: 3–4 tiers (primary gradient-filled, secondary
  outlined-on-white, ghost, and a "light" variant for use on dark
  backgrounds), all sharing one hover physics (a slight lift with a bouncy
  easing curve) and one focus-ring treatment.
- **Cards**: white surface, hairline border, minimal resting shadow that
  deepens and lifts on hover — used identically for feature cards, data
  cards, and step cards.
- **Pills/badges/tags**: rounded-full, small-caps or all-caps label with a
  colored dot, used for both static category labels and interactive filter
  toggles (toggle state shown via fill + `aria-pressed`, not color alone).
- **Progressive-disclosure accordions**: plain `<details>/<summary>`
  semantics styled to match cards, so they work without JS and keep native
  accessibility.
- **Explainability strip**: a horizontal (wrapping to vertical on mobile)
  chain of labeled steps connected by arrows — a reusable pattern for showing
  "how a result was derived" wherever a computed output needs to justify
  itself.
- **Status/warning callouts**: left-accent-bordered, icon+text banners in the
  semantic-color set, distinct from generic informational text so
  cautionary content doesn't blend into body copy.

## Layout & responsiveness

- Content is capped to a max width and centered, with fluid padding via
  `clamp()` rather than fixed breakpoint jumps for spacing.
- One collapse point turns a horizontal nav into a hamburger-triggered
  dropdown panel; a second, tighter breakpoint simplifies the header further
  (hides secondary brand text, collapses installed-app affordances).
- Grids collapse in predictable steps: 4-across → 2-across → 1-across, never
  skipping straight to single-column from four.

## Motion & accessibility posture

- Motion is used to *reinforce meaning* (a pulse when a filter is toggled, a
  lift on hover to signal interactivity, a scroll-reveal for narrative
  pacing) rather than decoration for its own sake — and every animation has a
  `prefers-reduced-motion` fallback that simply shows the end state.
- Semantic HTML and ARIA states are treated as load-bearing (skip links,
  `aria-pressed`/`aria-expanded`, focus-visible outlines in a high-contrast
  accent) rather than retrofitted.
- Empty/error/offline states are designed, not defaulted — every async
  surface has a considered "nothing here yet," "this failed," and "you're
  offline" treatment.

## Implications for a redesign

Because the identity lives in a small set of tokens (palette, radius scale,
shadow scale, one typeface) and a handful of reused component patterns
rather than bespoke one-off styling per page, broad restyling is possible by
changing the token layer and a dozen shared component rules — page-level HTML
structure and content don't need to move, and nothing here touches data flow,
so it's naturally backend-safe. The riskiest piece to redesign carefully is
the hand-built hero illustration and its interaction script, since it's
bespoke SVG/JS rather than a swappable component.
