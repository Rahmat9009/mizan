# SPEC.md

# 1. Product Summary

Build a high-end web application for an agentic portfolio intelligence and governance system.

The platform connects market-intelligence agents to a deterministic safety and execution layer.

Full system:

Market Research Agent
→ Stock Selection Agent
→ Probability / Confidence Agent
→ Trader Agent
→ TradeProposal
→ Deterministic Risk Engine
→ AI Risk Agent
→ Portfolio Governor
→ Execution Gate
→ Alpaca Paper
→ Order / Fill Reconciliation
→ Persistent Audit Timeline

The frontend must make this entire lifecycle observable and understandable.

---

# 2. Product Objective

Enable users to benefit from autonomous trading agents without giving AI unrestricted control over portfolio execution.

The application must answer:

1. What is the agent proposing?
2. Why is it proposing it?
3. What deterministic risk rules apply?
4. What contextual risks were identified?
5. What did the Governor decide?
6. Was quantity changed?
7. Is execution authorized?
8. What happened at the broker?
9. Can the complete decision be audited later?

---

# 3. Target Users

## Individual Investor

Needs:

* understandable decisions
* clear portfolio status
* confidence that autonomous agents are bounded
* easy explainability
* simple controls

## Portfolio Manager / Risk Operator

Needs:

* information density
* risk policies
* interventions
* exposure
* audit history
* execution safeguards
* operational agent visibility

---

# 4. Application Routes

## /

Marketing / product landing page.

## /app

Combined Command Center.

## /app/proposals

Proposal list.

## /app/proposals/:proposalId

Proposal case file.

## /app/portfolio

Portfolio and exposure.

## /app/risk

Risk Center.

## /app/orders

Order lifecycle list.

## /app/orders/:clientOrderId

Order detail/lifecycle.

## /app/audit

Forensic audit timeline.

## /app/agents

Agent operations center.

## /app/settings

System configuration.

---

# 5. Global Shell

Application shell includes:

* sidebar navigation
* product logo/name
* current environment indicator
* autonomy mode
* PAPER status
* notification center
* theme toggle
* user/account control

Persistent safety state should be visible without navigating away.

Example:

PAPER ONLY

AUTONOMY: MANUAL

KILL SWITCH: OFF

---

# 6. Dashboard

## Header Metrics

Display:

* equity
* cash
* buying power
* daily P&L
* active risk
* autonomy mode
* execution status
* kill switch

Each metric includes source/provenance where applicable.

---

## Agent Pipeline

Primary dashboard centerpiece.

Stages:

Research
Selection
Probability
Trader
Hard Risk
AI Risk
Governor
Execution

States:

idle
running
passed
warning
reduced
blocked
error
complete

Selecting a stage exposes details.

---

## Active Proposals

Show:

symbol
side
quantity
confidence
current stage
Governor status
time
provenance

---

## Risk Alerts

Display:

severity
risk type
proposal/symbol
reason
affected policy
timestamp

---

## Recent Activity

Include:

* proposals
* Governor decisions
* execution gates
* orders
* fills
* audit events

---

# 7. Proposal Case File

## Intelligence Panel

Display:

instrument
side
proposed quantity
estimated price
strategy confidence
thesis
invalidation condition
research summary
source agents
provenance

---

## Governance Panel

Display:

deterministic risk checks
AI contextual review
Governor decision
original quantity
approved quantity
risk score
execution state

---

## Decision Change Visualization

Example:

PROPOSED
40

↓ Governor intervention

APPROVED
20

Reason:
Portfolio concentration would exceed policy.

---

## Timeline

Show full sequence from research through execution.

---

# 8. Portfolio

## Summary

equity
cash
buying power
daily P&L
realized P&L
unrealized P&L
capital at risk

## Positions Table

Columns:

instrument
side
quantity
market value
cost
price
P&L
weight
risk contribution
source strategy

## Exposure

Provide visualizations for:

* symbol concentration
* risk allocation
* asset type
* strategy
* defined options risk

Only display sector/theme/correlation metrics when supported by real or explicitly labeled upstream data.

Do not fabricate unavailable analytics.

---

# 9. Risk Center

## Policy

Provide editable/readable policy cards.

Examples:

Max Trade Allocation
Max Position Concentration
Daily Drawdown Floor
Minimum Liquidity
Maximum Volatility
Options Defined Loss
Maximum Contracts
Minimum DTE

---

## Risk Utilization

Show policy utilization with restrained visual bars/gauges.

Example:

Portfolio Defined Risk

3.2% / 5.0%

---

## Interventions

Table:

proposal
symbol
intervention
rule
before
after
time

Possible interventions:

REDUCE
REJECT
BLOCK
REAUTHORIZE
MARKET CLOSED

---

## Safety Controls

Display prominently:

PAPER ONLY

Execution:
ENABLED / DISABLED

Dry Run:
ON / OFF

Kill Switch:
ON / OFF

Autonomy:
OBSERVE / MANUAL / AUTONOMOUS PAPER

---

# 10. Orders

## Order List

Columns:

instrument
side
proposed quantity
approved quantity
broker status
fill progress
submitted time
client order ID
proposal

## Status Vocabulary

WOULD SUBMIT
SUBMITTED
NEW
PARTIALLY FILLED
FILLED
REJECTED
CANCELED
EXPIRED

## Order Detail

Show:

authorization
idempotency
fresh-risk validation
asset validation
market validation
broker submission
fills
reconciliation events

---

# 11. Audit

## Timeline

Chronological events.

Each event:

timestamp
actor
action
summary
state
source
linked proposal
linked order

## Filters

proposal
symbol
agent
action
decision
broker status
date range

## Decision Replay

Animated/step-driven reconstruction of:

Research
→ Proposal
→ Risk
→ AI Review
→ Governor
→ Execution
→ Broker

Provide pause/step controls rather than forcing continuous playback.

---

# 12. Agents

Agent cards:

Research Agent
Stock Selection Agent
Probability Agent
Trader Agent
Risk Engine
AI Risk Agent
Portfolio Governor
Execution Agent

Card information:

status
current action
last action
latency
last update
proposal
confidence/risk metric
recent events

Allow drill-down.

Do not use personified/cartoon visualizations.

---

# 13. Settings

## Autonomy

Observe
Manual
Autonomous Paper

## Execution

enabled
dry run
kill switch

## Risk Policies

all configured hard-risk values.

## Connections

Alpaca Paper
AI Model
Upstream Agents

Status only:

CONNECTED
DEGRADED
NOT CONFIGURED
ERROR

Do not display credentials.

## Appearance

Dark / Light
Density
Reduced Motion
Timezone

## Diagnostics

database
audit storage
broker connectivity
agent status
backend health

---

# 14. Provenance System

Create a reusable ProvenanceBadge.

Possible values:

ALPACA PAPER
LIVE PORTFOLIO
LIVE AGENT
AI RISK MODEL
CALLER SUPPLIED
DEMO
SYNTHETIC

Every important metric should have an understandable source.

Do not prominently expose Featherless.

---

# 15. Design System

## Overall

premium
institutional
modern
restrained
high trust

## Dark Theme

Background:
deep charcoal/navy.

Panels:
slightly elevated dark surfaces.

Borders:
thin neutral lines.

Typography:
bright primary text, muted secondary text.

## Light Theme

warm/cool off-white rather than pure white where visually appropriate.

Maintain equal semantic clarity.

---

# 16. Semantic Colors

APPROVE / healthy:
green

REDUCE / watch:
amber

REJECT / critical:
red

Neutral/pending:
gray or muted blue

No large rainbow palette.

---

# 17. Typography

Use a modern sans-serif suitable for dense financial interfaces.

Strong hierarchy:

large editorial landing-page display type

clean application headings

compact numerical/table typography

Monospaced font may be used selectively for:

* client order IDs
* timestamps
* proposal IDs
* structured payloads

---

# 18. Tables

Tables must support:

sorting
filters
sticky headers where useful
hover/focus state
row drill-down
responsive degradation

Do not make every table visually heavy.

---

# 19. Landing Page

## Section 1 — Hero

Headline around:

The control layer between AI agents and your portfolio.

Subheading explains:

agents research and propose trades while deterministic governance controls risk and execution.

CTA:

Launch Command Center

Secondary CTA:

See How It Works

Hero visual:

animated agent pipeline feeding a realistic command-center preview.

---

## Section 2 — Problem

Explain the issue:

autonomous trading agents can generate increasingly sophisticated decisions but direct broker access creates uncontrolled risk.

---

## Section 3 — Intelligence

Visualize:

Research
→ Selection
→ Probability
→ Trader

Show that the system uses multiple specialized agents.

---

## Section 4 — Handoff

Visualize the TradeProposal as a structured object transitioning from intelligence into governance.

---

## Section 5 — Governance

Show:

Hard Risk
→ AI Risk
→ Governor

Highlight:

AI cannot override deterministic limits.

---

## Section 6 — Decision Demo

Three proposal cards.

One:

APPROVE

One:

REDUCE

One:

REJECT

Use scroll interaction to show why each outcome happened.

---

## Section 7 — Execution

Visualize:

Authorization
→ Fresh Risk
→ Market
→ Idempotency
→ Alpaca Paper

Show kill-switch and paper-only controls.

---

## Section 8 — Command Center

Large realistic product preview.

---

## Section 9 — Options

Explain:

defined-risk options
maximum-loss risk measurement
structure fingerprinting
no naked shorts
Governor quantity reduction only

---

## Section 10 — Audit

Visualize Decision Replay.

Show the entire lifecycle remaining explainable after execution.

---

## Section 11 — Autonomy

Three cards:

OBSERVE

MANUAL APPROVAL

AUTONOMOUS PAPER

---

## Section 12 — CTA

Launch Command Center.

---

# 20. Landing Motion

Use motion to explain architecture.

Recommended interactions:

* pipeline stages activate progressively
* TradeProposal physically moves between intelligence and governance
* quantity animates during REDUCE
* REJECT stops before execution
* APPROVE continues into Alpaca Paper
* audit trail appears behind the execution
* dashboard sections transition subtly as user scrolls

Do not build animation purely for spectacle.

---

# 21. Application Motion

Minimal.

Allowed:

* panel transitions
* status changes
* number transitions
* timeline expansion
* drawer/modal movement
* notification entry
* pipeline progress

Operational areas should not constantly animate.

---

# 22. Accessibility

Must include:

keyboard support
focus indicators
semantic status labels
screen-reader labels
high contrast
reduced-motion mode
dark/light parity

---

# 23. Responsive Layout

## Desktop

Primary target.

Full command center.

## Tablet

Two-column adaptation.

## Mobile

Single-column dashboard.

Tables convert into:

compact rows/cards
+
detail drawers.

Maintain monitoring and safety controls on mobile.

---

# 24. Data Integrity

Never silently present synthetic information as live.

Unavailable data should appear as:

Unavailable

Not:

0

unless zero is the actual value.

Use provenance consistently.

---

# 25. Security Presentation

Never show:

API keys
secret keys
authorization tokens
raw authentication headers

Connection state only.

---

# 26. API Integration Direction

Frontend should consume backend endpoints for:

health
portfolio
proposal evaluation
proposal lifecycle
execution
orders
reconciliation
audit
agents/status

Keep UI state independent from raw Alpaca SDK response structures.

---

# 27. Component Direction

Likely reusable components:

AppShell
Sidebar
TopStatusBar
MetricCard
RiskBadge
DecisionBadge
ProvenanceBadge
AgentCard
AgentPipeline
TradeProposalCard
DecisionTimeline
RiskRuleRow
GovernorDecisionPanel
ExecutionGatePanel
OrderLifecycle
AuditEvent
DecisionReplay
PositionTable
RiskUtilization
KillSwitch
AutonomySelector
NotificationCenter
CommandPalette
ThemeToggle

---

# 28. Product Quality Bar

The application must not look like a generated dashboard template.

Avoid:

* generic four-card SaaS layout everywhere
* excessive rounded cards
* gradient overload
* arbitrary icons
* oversized hero text with no actual product
* fake charts
* meaningless “AI insights”
* excessive blur
* animated glowing backgrounds

Every visual element should support:

decision-making
risk comprehension
explainability
system trust

---

# 29. Reference Design Direction

Baseline reference category:

high-end fintech / infrastructure / operational software.

Desired qualities inspired by:

Apple:
motion restraint and storytelling

Stripe:
product polish and hierarchy

Institutional trading software:
information clarity and density

Do not clone any single product 1:1.

---

# 30. Final Product Principle

The user should understand the system within seconds:

Market agents generate opportunities.

The Risk Engine establishes hard boundaries.

The AI Risk Agent adds contextual skepticism.

The Governor makes the final portfolio decision.

The Execution Gate ensures the authorization remains safe.

Alpaca Paper executes.

Everything is recorded.

AI may act autonomously.

AI may not bypass governance.
