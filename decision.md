# DECISIONS.md

## Product

Build a premium web application for an AI-powered trading system that combines:

### Intelligence Layer

Owned primarily by Faisal's upstream agent system:

Research Agents
→ Stock Selection Agent
→ Probability / Confidence Agent
→ Trader Agent
→ TradeProposal

### Governance Layer

Risk, governance, execution, and audit system:

TradeProposal
→ Deterministic Risk Engine
→ AI Risk Agent
→ Portfolio Governor
→ Execution Safety Gate
→ Alpaca Paper Execution
→ Order / Fill Reconciliation
→ Persistent Audit Timeline

The product is not positioned as another AI stock-picking bot.

Core positioning:

> The control layer between AI agents and your portfolio.

Core conceptual distinction:

> Intelligence asks: “What should we trade?”

> Governance asks: “Should this trade be allowed, at what size, and can it safely reach the broker?”

---

# Primary Users

Two equally important primary user groups:

1. Individual investors / serious traders
2. Portfolio managers / risk operators

The interface must therefore be:

* approachable enough for an individual investor
* powerful enough for a professional operator
* transparent enough for risk supervision
* visually clear enough for hackathon judges/demo viewers

---

# Primary Dashboard Model

Use a Combined Command Center.

The dashboard must combine:

* portfolio health
* live agent activity
* risk alerts
* execution controls
* current proposals
* recent broker/order activity

Primary dashboard visual hierarchy:

## Top Strip

* Equity
* Daily P&L
* Cash
* Buying Power
* Portfolio Risk
* Autonomy Mode
* Execution State
* Kill Switch

## Main Center

End-to-end live pipeline:

Research
→ Selection
→ Probability
→ Trader
→ Hard Risk
→ AI Risk
→ Governor
→ Execution

## Left / Secondary Panel

* active TradeProposals
* recently evaluated trades
* APPROVE / REDUCE / REJECT outcomes

## Right Panel

* risk alerts
* blocked trades
* execution warnings
* stale authorization warnings
* market closed state
* kill-switch state

## Bottom Area

* current positions
* orders/fills
* recent audit events

---

# Decision Pipeline Visualization

Use a hybrid representation.

## Compact View

Horizontal pipeline:

Research
→ Selection
→ Probability
→ Trader
→ Risk
→ Governor
→ Execution

## Detailed View

Selecting a proposal opens a chronological vertical decision timeline.

Each step should show:

* actor / agent
* input
* reasoning or rule result
* output
* status
* timestamp
* provenance
* resulting quantity/risk change

---

# Visual Personality

Use a hybrid visual identity:

Premium fintech

* institutional trading/risk-desk structure
* restrained AI command-center elements.

Avoid:

* noisy crypto-terminal aesthetics
* excessive neon
* generic AI gradients
* glowing AI orbs
* cartoon AI agents
* cluttered sci-fi HUD interfaces
* gratuitous glassmorphism

Reference feeling:

Apple × Stripe × institutional trading platform.

---

# Theme

Default:

Dark mode.

Also provide:

Fully supported light mode.

Dark theme direction:

* deep charcoal / very dark navy surfaces
* subtle layered elevation
* thin borders
* crisp typography
* restrained accent color
* high-contrast financial/status information

Do not use pure black everywhere.

---

# Information Density

Adaptive density.

Default interface:

clean and readable.

Additional information appears through:

* expandable cards
* drawers
* popovers
* drill-down views
* tabs
* detailed proposal timelines

The main dashboard must not immediately overwhelm users.

---

# Risk Color System

Use a neutral professional interface foundation.

Reserve strong semantic colors for actual decisions and risk states.

Green:

* approved
* healthy
* completed
* within policy

Amber:

* reduced
* warning
* watch
* intervention

Red:

* rejected
* blocked
* critical
* execution prevented

Neutral:

* informational
* pending
* inactive

Color must never be the only status indicator.

Use labels/icons/text as well.

---

# Explainability

Risk decisions must be interactive and inspectable.

Do not display only:

APPROVE
REDUCE
REJECT

Users must be able to see:

* exact deterministic rules
* AI risk concerns
* original quantity
* approved quantity
* before/after risk impact
* Governor rationale
* responsible stage
* execution readiness
* linked audit events

Example:

40 shares proposed
→ 20 approved

UI should explain exactly why.

---

# Autonomy Modes

Support three operating modes.

## Observe

Analysis and auditing only.

Never execute.

## Manual Approval

System evaluates and Governor decides.

Human manually confirms execution.

## Autonomous Paper

Governor-approved trades may automatically proceed through the execution safety system.

Still subject to:

* deterministic risk policy
* fresh portfolio validation
* stale authorization checks
* market-hours checks
* asset validation
* idempotency
* kill switch
* Alpaca Paper only

Live trading is not supported.

---

# Main Navigation

Use:

Dashboard
Proposals
Portfolio
Risk Center
Orders
Audit
Agents
Settings

---

# Route Structure

## Public

/

Premium product landing page.

## Application

/app

Main command center.

Suggested routes:

/app
/app/proposals
/app/proposals/:id
/app/portfolio
/app/risk
/app/orders
/app/orders/:id
/app/audit
/app/agents
/app/settings

---

# Landing Page Positioning

The landing page must emphasize governance first.

Primary message direction:

> The control layer between AI agents and your portfolio.

Secondary story:

AI agents can:

research
select
analyze
propose

but cannot bypass:

risk
governance
authorization
execution safety

---

# Hero

Use a hybrid centerpiece.

An animated end-to-end agent pipeline flows into a realistic command-center interface.

Show visually:

Research
→ Stock Selection
→ Probability
→ Trader
→ Trade Proposal
→ Hard Risk
→ AI Risk
→ Governor
→ Execution

The hero should demonstrate movement of a trade through the system.

Possible visible outcomes:

APPROVE
REDUCE
REJECT

---

# Landing Page Story

Use this order:

1. Hero / positioning
2. The autonomous-trading problem
3. Multi-Agent Market Intelligence
4. TradeProposal handoff
5. Portfolio Governance
6. APPROVE / REDUCE / REJECT live decision demonstration
7. Controlled Execution
8. Portfolio Command Center
9. Risk Center
10. Options Governance
11. Auditability / Decision Replay
12. Autonomy Modes
13. Final CTA

---

# Intelligence Layer Presentation

Faisal's work must be visible throughout the product.

Agent stages:

Market Research Agent
→ Stock Selection Agent
→ Probability / Confidence Agent
→ Trader Agent

Do not make the application appear to begin at the Risk Engine.

The TradeProposal is the boundary between:

market intelligence

and

portfolio governance.

---

# Agents Page

Use an operations-center layout.

Agent cards:

* Market Research Agent
* Stock Selection Agent
* Probability / Confidence Agent
* Trader Agent
* Deterministic Risk Engine
* AI Risk Agent
* Portfolio Governor
* Execution Agent

Each card may display:

* ACTIVE / IDLE / ERROR
* current task
* last action
* current proposal
* confidence where appropriate
* risk score where appropriate
* latency
* last heartbeat
* recent decisions
* expandable logs

Do not use cartoon avatars.

---

# Data Provenance

Display provenance transparently throughout the product.

Possible tags:

ALPACA PAPER
LIVE AGENT OUTPUT
AI RISK MODEL
DEMO DATA
SYNTHETIC
CALLER SUPPLIED
LIVE PORTFOLIO

Do not expose Featherless branding in the normal user interface.

The underlying AI provider is an implementation detail.

Provider details may exist only in:

* developer diagnostics
* advanced Settings
* internal system information

---

# Proposal Detail View

Use a split-screen case-file layout.

## Left

Trade intelligence:

* symbol / instrument
* BUY / SELL
* quantity
* estimated price
* confidence
* investment thesis
* invalidation condition
* upstream research
* source agents
* provenance

## Right

Governance:

* deterministic risk checks
* AI contextual risk analysis
* Governor decision
* original quantity
* approved quantity
* risk score
* execution state
* linked audit events

## Top

Compact pipeline:

Research
→ Selection
→ Probability
→ Trader
→ Risk
→ Governor
→ Execution

---

# Portfolio Page

Organize into four major layers.

## Overview

* equity
* cash
* buying power
* daily P&L
* realized P&L
* unrealized P&L
* aggregate risk-at-risk

## Positions

* symbol / strategy
* side
* quantity
* market value
* cost / entry
* current price
* P&L
* portfolio weight
* risk contribution

## Exposure

* symbol exposure
* theme/sector concentration where available
* correlated exposure where available
* equity exposure
* options exposure
* defined maximum loss

## Risk Allocation

Show how much portfolio risk is consumed by each position relative to policy limits.

For options:

emphasize maximum defined loss.

Do not present stock-equivalent notional as the primary risk measure for defined-risk strategies.

---

# Risk Center

Create four areas.

## Policy Limits

* maximum trade size
* maximum position concentration
* daily drawdown limit
* liquidity threshold
* volatility threshold
* options maximum-defined-loss limit
* maximum contracts
* minimum DTE
* strategy allowlist

## Current Risk

* portfolio risk usage
* concentration
* drawdown
* options defined-loss exposure
* active warnings
* limit utilization

## Recent Interventions

* Governor reductions
* hard-policy rejections
* stale authorizations
* market-closed blocks
* execution blocks
* reauthorization-required events

## Controls

* autonomy mode
* execution enabled/disabled
* dry-run state
* kill switch
* paper-only status

---

# Orders Page

Use lifecycle-oriented order management.

Order table fields:

* symbol / instrument
* side
* proposed quantity
* Governor-approved quantity
* broker status
* submitted time
* filled quantity
* average fill price
* client order ID
* source proposal

Clearly differentiate:

DRY RUN
WOULD SUBMIT
SUBMITTED
PARTIALLY FILLED
FILLED
REJECTED
CANCELED
EXPIRED

Clicking an order opens a lifecycle drawer.

Example:

Proposal
→ Approved
→ Authorized
→ Submitted
→ Partially Filled
→ Filled

Also expose:

* Alpaca Paper state
* idempotency result
* fresh-risk result
* execution gate result
* linked audit events

---

# Audit Page

Use a forensic timeline.

Filters:

* proposal
* symbol
* agent
* decision
* execution state
* date/time
* APPROVE
* REDUCE
* REJECT
* DRY RUN
* SUBMITTED
* FILLED

Pipeline view:

Research
→ Selection
→ Probability
→ Trader
→ Risk Engine
→ AI Risk
→ Governor
→ Execution
→ Broker

Each event shows:

* timestamp
* actor
* action
* relevant inputs
* result
* quantity/risk changes
* provenance
* linked proposal/order

Allow sanitized structured payload inspection.

Never expose credentials.

---

# Decision Replay

The Audit page includes a Decision Replay feature.

It visually reconstructs a selected proposal from:

research signal

through:

broker outcome.

This should be one of the product's signature demo features.

---

# Settings

Use five groups.

## Trading & Autonomy

* Observe
* Manual Approval
* Autonomous Paper
* execution state
* dry run
* kill switch

## Risk Policy

* trade limits
* concentration
* drawdown
* liquidity
* volatility
* options max-loss policy
* contract limits
* DTE policy

## Connections

* Alpaca Paper
* AI model
* upstream agents

Show only connection state.

Never reveal saved credentials.

## Interface

* dark/light theme
* density
* notifications
* timezone

## Safety & System

* PAPER ONLY
* database health
* audit health
* execution diagnostics
* backend/system version

---

# Notifications

Prioritize high-value events only.

Notify for:

* trade approved
* trade reduced
* trade rejected
* execution blocked
* stale authorization
* market closed
* kill switch activated
* order submitted
* partial fill
* order filled
* system/agent/provider error

Initial implementation:

in-app notifications.

Optional future channels:

browser
email
Slack

---

# Landing Page Motion

Use balanced cinematic motion.

Allow:

* scroll-driven hero pipeline
* cards moving through the decision pipeline
* sticky storytelling sections
* APPPROVE / REDUCE / REJECT transformations
* dashboard preview transitions
* subtle parallax
* number transitions
* state transitions
* controlled depth

Avoid:

* continuous distracting dashboard animation
* excessive cursor gimmicks
* overly long animations
* motion without explanatory purpose

Heavier animations belong primarily on `/`.

The application workspace should remain calm.

---

# Reduced Motion

Respect:

prefers-reduced-motion

Every important piece of content must remain understandable without animation.

---

# Options

Support presentation for:

* long call
* long put
* vertical debit spread
* vertical credit spread
* iron condor

Important options safety concepts to surface:

* maximum defined loss
* contract quantity
* expiry
* DTE
* strategy structure
* premium/debit/credit
* Governor-approved contracts

Naked short structures are not supported.

The frontend should visually reinforce:

Defined risk.

---

# Responsive Strategy

Desktop-first for the operational command center, but fully responsive.

Desktop:
full multi-column dashboard.

Tablet:
collapse secondary panels.

Mobile:
stack cards and convert dense tables into responsive rows/detail drawers.

Do not attempt to reproduce a desktop trading terminal at mobile width.

---

# Accessibility

Minimum goals:

* WCAG-conscious contrast
* keyboard navigation
* visible focus states
* semantic HTML
* screen-reader labels
* no color-only status communication
* reduced-motion support
* usable light and dark modes

---

# Branding

Do not prominently brand infrastructure providers.

Product-facing language:

AI Risk Model
Portfolio Governor
Execution Agent
Market Intelligence Agents
Alpaca Paper

Avoid provider-specific AI branding in normal views.

---

# Product Tone

Professional.

Concise.

Calm.

Evidence-oriented.

Avoid exaggerated language such as:

“guaranteed profits”

“never lose”

“perfect AI trader”

The interface should communicate:

control
risk awareness
transparency
autonomy with boundaries

---

# Final Design Principle

The interface must constantly make one fact understandable:

AI agents can generate ideas.

They cannot bypass governance.
