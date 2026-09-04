# Reference research — Step 2 frontend baseline

Research done before any code was written, to decide what this product's interface
should borrow, what it should refuse, and why. Nothing here is copied: no
proprietary text, asset, palette or component was reproduced. The output is a
list of *patterns* and a set of layout decisions we adopted or rejected.

Sources consulted (Sept 2026):

- [Linear — How we redesigned the Linear UI](https://linear.app/now/how-we-redesigned-the-linear-ui)
- [Linear — A calmer interface for a product in motion](https://linear.app/now/behind-the-latest-design-refresh)
- [Linear design breakdown (925 Studios)](https://www.925studios.co/blog/linear-design-breakdown-saas-ui-2026)
- [Fintech dashboard design: 9 real products analysed](https://adminlte.io/blog/fintech-dashboard-design-examples/)
- [Trading app design: UI, UX & system architecture (Lollypop)](https://lollypop.design/blog/2026/june/trading-app-design/)
- [Sidebar navigation design patterns for web applications](https://girardmedia.com/blog/sidebar-navigation-design-web-applications)
- [Stripe design system — tokens and structure](https://www.designsystems.one/design-systems/stripe-design)
- [Ramp design system reference](https://styles.refero.design/style/b38702a0-75ab-474c-9106-00b624535825)
- [Dashboard design patterns for web apps](https://artofstyleframe.com/blog/dashboard-design-patterns-web-apps/)

---

## 1. Patterns worth taking

### Application shell

| Pattern | Where it comes from | What we took |
| --- | --- | --- |
| Persistent left sidebar, 240–300px, grouped links | Linear, Vercel, Stripe Dashboard | 244px fixed rail, links grouped **Operations / Governance / System** so the product's two halves are visible in navigation itself |
| Command palette as the real navigation for power users | Linear | ⌘K palette that searches proposals and client order IDs, not only views |
| Thin top bar carrying context, not chrome | Linear, Vercel | 52px bar: view name, market state, environment badge, autonomy mode, search, notifications, theme |
| Not every element carries equal weight; supporting UI recedes | Linear's redesign notes | Navigation and labels sit at secondary/tertiary text tokens; only values and status carry full contrast |
| 4px spacing grid throughout | Linear | `--sp-1 … --sp-24`, all multiples of 4 |

### Density and tables

| Pattern | Source | What we took |
| --- | --- | --- |
| ~36px rows, minimal chrome, no zebra striping | Linear | 38px comfortable / 30px compact rows, hairline row borders only |
| Tabular figures, consistent precision, right-aligned numerics | Institutional trading UI convention | `font-variant-numeric: tabular-nums` on every cell by default; numeric columns right-aligned |
| Large high-contrast primary number, muted secondary metrics | Trading platform guidance | Metric cells: display-face value, uppercase micro-label, tertiary note line |
| Colour reserved strictly for financial/decision state | Fintech dashboard analysis | Chrome is near-monochrome; green/amber/red appear only on decisions, risk states and P&L |
| Explicit pending / in-flight states | Trading platform guidance | Order lifecycle vocabulary is a first-class filter row, and "not evaluated" renders as a dash, never a tick |

### Theming

| Pattern | Source | What we took |
| --- | --- | --- |
| Dark mode designed first, light mode as a peer, both from tokens | Linear, Supabase, Vercel | Two complete semantic token sets; no component owns a colour value |
| Few theme variables, derived rather than enumerated | Linear's move to three theme variables | Two layers: raw palette (`--p-*`) and semantic roles; components may only use the semantic layer |
| Multi-layer, slightly tinted elevation shadows | Stripe | Two-layer shadows tuned per theme rather than a single flat drop |
| Conservative radii (4–8px) | Stripe | 2/3/5/8px scale; only status chips are pills |

### Structure and storytelling

| Pattern | Source | What we took |
| --- | --- | --- |
| Lead with one number, not a wall of data | Mercury/Ramp/Brex analysis | Landing hero leads with a claim and one worked case (40 → 20), not a stat grid |
| Editorial display type with tight leading against a plain body face | Stripe | Archivo for display, IBM Plex Sans for UI, IBM Plex Mono for identifiers |
| Configurable density with preset layouts | Multi-asset platform guidance | Comfortable/compact density that changes rhythm only, never type size |

---

## 2. Patterns we deliberately rejected

**The four-metric card row.** Present in nearly every dashboard reference. Four
floating rounded cards imply four separate concerns; equity, cash, buying power
and capital at risk are read *together*. We use one bordered strip divided by
hairlines — an instrument cluster, not a card grid.

**Sidebar-as-settings.** Most references push environment and safety switches
into a settings page. For a product whose entire claim is "AI cannot bypass
governance", paper-only status, autonomy mode and the kill switch must never be
more than zero clicks away. They are pinned to the bottom of the sidebar and
never scroll out of reach.

**Colour-only status.** Standard across trading dashboards. Every status here
carries a word *and* a marker shape that differs per tone (circle / diamond /
square / bar), so the interface survives greyscale and colour-blindness.

**Charts as page furniture.** Reference dashboards fill space with sparklines and
donuts. We have no market-data feed, so a price chart would be fiction. There is
not a single time-series chart in this baseline. Proportions that we *can*
compute honestly (position weight, policy utilisation, fill progress) are drawn
as bars, and nothing else is drawn at all.

**Glass, glow and gradient depth.** Common in "AI product" styling. Used nowhere
except two 8px backdrop blurs on sticky bars. No glowing orb, no animated
gradient, no personified agent.

**Ambient animation in operational views.** The only looping animation in the
whole application is a slow ring on a pipeline stage that is genuinely running,
and it stops entirely under `prefers-reduced-motion`.

**Zero as a stand-in for missing data.** Several fintech references show `0.00`
where a value could not be fetched. Here an unavailable value renders the word
`Unavailable` with a tooltip explaining why, and the backend's own `daily_pnl_pct:
None` semantics are preserved in the type system via `Sourced<T>`.

**Provider branding.** The AI infrastructure provider is never named in the UI.
Product-facing language only: *AI Risk Model*, *Portfolio Governor*, *Execution
Agent*, *Alpaca Paper*.

---

## 3. Layout decisions adopted, and why they fit this product

**The governance boundary as a recurring device.** A single brass hairline marks
where market intelligence ends and portfolio governance begins. It appears in the
`AgentPipeline` (between Trader and Hard Risk, labelled *TradeProposal*), down the
middle of the proposal case file, between the two agent groups on the Agents page,
and in the landing handoff section. It is the only decorative colour in the
product and it always carries a label. It exists because the boundary is the
product's actual thesis, and a reader should be able to see it without reading a
word of copy.

**The pipeline as a full-width instrument band, not a card.** On the dashboard the
eight stages span the content column directly beneath the metric strip. Making it
a card in a grid would imply it is one widget among several; it is the spine of
the system.

**Split-screen case file.** Left column is what the agents claimed; right column
is what policy did about it. The brass rule runs down the centre. Collapsing to
one column below 1100px keeps the boundary as a horizontal rule so the two halves
never merge into one undifferentiated list.

**The quantity ledger.** `40 → 20` with the original struck through and kept
legible. Replacing the proposed number with the approved one would hide exactly
the fact the product exists to show.

**Equity and options risk kept structurally separate.** Not just different rows —
different tabs, with different limit vocabulary. A share position risks its market
value; a defined-risk spread risks its maximum loss. The Risk Center never adds
those into one figure, and the Portfolio page labels the risk basis on every row.

**Lifecycle-first orders.** The filter row *is* the state vocabulary
(`DRY RUN → WOULD SUBMIT → SUBMITTED → NEW → PARTIALLY FILLED → FILLED / REJECTED /
CANCELED / EXPIRED`), so an operator learns the model before reading a single row.
Both the internal lifecycle state and the raw broker word are shown, because
conflating them hides reconciliation problems.

**Mobile keeps safety, drops density.** Below 900px wide tables become stacked
cards with the same data. The environment badge, autonomy selector and kill
switch survive to the narrowest breakpoint; the view title and the ⌘K affordance
are what get cut, because a phone has neither the room nor the shortcut.

---

## 4. Typography rationale

Archivo (display) + IBM Plex Sans (UI) + IBM Plex Mono (identifiers) was chosen
over the Inter-everywhere default that most of the references use. Plex was drawn
for an engineering institution and reads as instrumentation rather than as a
startup; Archivo's tighter, more industrial grotesque gives page and hero titles a
signage quality that Inter Display does not. Plex Mono carries client order IDs,
proposal IDs, symbols, rule names and timestamps — the things an operator copies,
greps or quotes in an incident — while tabular figures handle everything numeric
in the sans.
