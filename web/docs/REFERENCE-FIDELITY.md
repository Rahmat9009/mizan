# Reference fidelity reconstruction

Baseline hierarchy for the application surfaces:

| Surface | Reference owner |
| --- | --- |
| Application shell, dashboard, pipeline/trace, proposals, portfolio density, agents, settings | Galileo |
| Risk Center, policy, intervention history, audit, decision replay | Fiddler |
| Orders, broker/execution state, PAPER environment, fills | Alpaca |
| Landing pacing | Guardrails |

Our identity is preserved inside those systems: brass governance boundary, the
40 → 20 quantity ledger, PAPER ONLY, Decision Replay, equity/options risk
distinction, deterministic vs AI risk separation, typed provenance, Unavailable
instead of fabricated values.

---

## RF-1 — Application shell

### How the comparison was made

`references/01-galileo/clone` was served statically alongside our dev server and
both were measured with `getComputedStyle` / `getBoundingClientRect` at a 1440px
viewport, rather than read off the ANALYSIS notes. Numbers below are measured,
not quoted.

### Measured differences

| # | Property | Galileo (measured) | Ours (measured, before) | Verdict |
| --- | --- | --- | --- | --- |
| 1 | Structural border colour | `#222636` — opaque, one value for panel edges, chrome edges and row rules | `rgba(255,255,255,0.07)` alpha | **Wrong kind of border.** An alpha hairline changes strength with whatever is behind it, so it read at ~`#1B1C20` on canvas and effectively vanished on the `#05070B` sidebar. Panels had no edge. |
| 2 | Chrome vs canvas | canvas `#090A0F` darkest, panels `#11131C` **lighter** | canvas `#090A0F`, sidebar `#05070B` **darker** | **Hierarchy inverted.** Our sidebar read as a hole cut in the page; Galileo's chrome sits at canvas level and lets data panels come forward. |
| 3 | Panel header | own band: `rgba(21,24,36,.8)` on a `#11131C` panel, `16px 24px`, 67px tall, real bottom border | same colour as panel body, no band, `16px 16px 12px` | Panel headers did not register as headers. |
| 4 | Panel radius | 12px large container / 8px cards | 8px everywhere | Acceptable; kept at 8px for an application (12px is a marketing-page radius). |
| 5 | Page gutter | 32px (`2rem`) | 24px | Too tight; content ran closer to the chrome than the reference. |
| 6 | Content max width | 1280px container, applied | `--content-max: 1560px` declared but **never applied** | Content sprawled edge-to-edge above 1560px. |
| 7 | Top chrome height | 65.6px nav | 52px topbar | Under-scaled; controls sat with 11px of breathing room. |
| 8 | Sticky chrome treatment | `rgba(9,10,15,.85)` + `backdrop-filter: blur(12px)` | fully opaque | Content cut off hard under the bar instead of passing beneath it. |
| 9 | Interactive control height | 34.4px (tabs), 33px (input) | 30px | Controls under-scaled relative to reference density. |
| 10 | Nav row height | n/a (no sidebar in reference); reference interactive rhythm is 34px | 30px | Raised to 32px to sit on the reference rhythm without losing rail compactness. |
| 11 | Operational label | 12px / 600 / uppercase / `letter-spacing: .05em` | 10px / 600 / uppercase / `.16em` | Ours was an editorial eyebrow, not an operational label — too small, too airy for a technical rail. |
| 12 | Active-state treatment | raised surface `#181B27` + 1px `#2D3348` border + white text | `rgba(255,255,255,.04)` fill + 2px brass left border + **brass icon** | Weak surface change, and brass leaked into routine navigation. |
| 13 | Mono usage | mono carries every ID, timestamp, latency and metric value | mono present, but chrome (search, clock) mixed | Chrome mono usage tightened. |
| 14 | Transition curve | `0.15s ease` | `150ms cubic-bezier(.22,1,.36,1)` | Overshoot curve on hover reads springy; reference chrome is non-elastic. |
| 15 | Responsive collapse | table scrolls horizontally below 768px | sidebar → overlay at 1080px, topbar sheds title at 760px, palette at 520px | **Already correct.** Verified: no horizontal overflow at 375px, autonomy select clamps to 104px, topbar holds 52px. Kept, re-tuned to the new gutter scale. |

### Corrections applied

1. **Border system rebuilt on opaque values.** `--border-hairline` → `#1E2230`
   (in-panel dividers), `--border-subtle` → `#222636` (Galileo's exact
   structural border — panel edges, chrome edges), `--border-strong` → `#2D3348`
   (active/emphasis). Every border now holds its strength over any surface. The
   light theme gets the matching three-step ramp.
2. **Surface hierarchy corrected.** Sidebar and topbar moved to `--bg-canvas`;
   separation now comes from the visible border, not from a darker well. Panels
   at `--bg-surface` read as raised, as in the reference.
3. **Panel treatment.** `.panel` takes the structural border; `.panel__head`
   gets its own band (`--bg-panel-head`) with symmetric padding and a real
   bottom rule.
4. **Top chrome.** 52 → 56px, gutter aligned to the page gutter, translucent
   with `backdrop-filter: blur(12px)` and an opaque `@supports` fallback,
   control heights 30 → 32px, tool cluster separated by a rule.
5. **Page canvas.** Gutter 24 → 32px on a `--page-gutter` token that steps down
   at each breakpoint; `--content-max` actually applied.
6. **Sidebar.** Brand block matched to 56px, nav rows 30 → 32px, group labels
   raised to the operational label spec (11px / `.08em`), groups spaced on a
   24px rhythm, active state rebuilt as raised surface + border.
7. **Brass discipline.** Brass removed from active nav icons. It survives in the
   product mark's gate and as a 2px structural marker on the active nav item —
   nothing else in the shell.
8. **Motion.** Added `--ease-ui` (`cubic-bezier(.4,0,.2,1)`) for chrome hover and
   state transitions; the overshoot `--ease-out` is now reserved for entrances.

### Deliberately not copied

Galileo's indigo→blue gradient CTA, gradient hero text, and glassmorphic
marketing nav. Those are landing-page devices and the brief rules out gradient
decoration; the fidelity we want from Galileo is structural.

### Verified after the change

Measured in the running app, dark and light, at 1440 / 1280 / 800 / 375.

| Element | Dark | Light |
| --- | --- | --- |
| Sidebar | `#090A0F`, right edge `#222636` | `#FAFAFA`, right edge `#E4E4E7` |
| Top bar | 56px, `rgba(9,10,15,.82)` + `blur(12px)`, edge `#222636` | 56px, `rgba(250,250,250,.82)` + `blur(12px)`, edge `#E4E4E7` |
| Panel | `#11131C` on `#090A0F`, edge `#222636` | `#FFFFFF` on `#FAFAFA`, edge `#E4E4E7` |
| Panel header band | `#151824` | `#F4F4F5` |
| Active nav row | 32px, `#151824` + `#2D3348` edge, brass tick `#C5A059` | 32px, `#FFFFFF` + `#C2C2C9` edge, brass tick `#967432` |
| Safety rail | `#151824` raised, kill switch inset `#05070B` | `#FFFFFF` raised, kill switch inset `#F4F4F5` |

Panel surface and edge in dark are now identical to the reference
(`#11131C` / `#222636`), and the panel header band matches its `#151824`.

Gutter: 32 → 24 → 24 → 16px across the four widths, with the top bar reading the
same token, so the view title stays on the content's left vertical. No
horizontal overflow at any width (`documentElement.scrollWidth` never exceeds
the viewport). The ≤1080px drawer was re-checked end to end: closed at
`translateX(-244px)` with `aria-hidden`, open at `translateX(0)` on
`--bg-surface`, `z-index: 70`, scrim present, and closing on scrim click.

Note on method: the browser pane was hidden for part of this pass, which
throttles the document — CSS transitions do not advance and captured frames go
stale. Verification is therefore by computed style and layout geometry rather
than by screenshot, which is exact for the properties changed here.
