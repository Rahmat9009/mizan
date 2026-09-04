import type { PipelineLayer, PipelineStage, PipelineStageId, StageState } from '@/types/domain';

/**
 * The eight stages, in the order a proposal travels them.
 *
 * The first four belong to the upstream market-intelligence system; the last
 * four belong to governance and execution. The boundary between `trader` and
 * `hard_risk` is where a TradeProposal stops being a suggestion.
 */
export const STAGE_ORDER: PipelineStageId[] = [
  'research',
  'selection',
  'probability',
  'trader',
  'hard_risk',
  'ai_risk',
  'governor',
  'execution',
];

export const STAGE_META: Record<
  PipelineStageId,
  { label: string; short: string; actor: string; layer: PipelineLayer; blurb: string }
> = {
  research: {
    label: 'Research',
    short: 'RES',
    actor: 'Market Research Agent',
    layer: 'intelligence',
    blurb: 'Gathers filings, price action and news into a candidate signal set.',
  },
  selection: {
    label: 'Selection',
    short: 'SEL',
    actor: 'Stock Selection Agent',
    layer: 'intelligence',
    blurb: 'Narrows the signal set to instruments worth sizing.',
  },
  probability: {
    label: 'Probability',
    short: 'PRB',
    actor: 'Probability / Confidence Agent',
    layer: 'intelligence',
    blurb: 'Scores each candidate and attaches a confidence to the thesis.',
  },
  trader: {
    label: 'Trader',
    short: 'TRD',
    actor: 'Trader Agent',
    layer: 'intelligence',
    blurb: 'Turns a scored candidate into a structured TradeProposal.',
  },
  hard_risk: {
    label: 'Hard Risk',
    short: 'HRD',
    actor: 'Deterministic Risk Engine',
    layer: 'governance',
    blurb: 'Applies fixed policy. Nothing downstream can raise these ceilings.',
  },
  ai_risk: {
    label: 'AI Risk',
    short: 'AIR',
    actor: 'AI Risk Model',
    layer: 'governance',
    blurb: 'Adds contextual scepticism. It may only tighten, never loosen.',
  },
  governor: {
    label: 'Governor',
    short: 'GOV',
    actor: 'Portfolio Governor',
    layer: 'governance',
    blurb: 'Decides the final size against the live portfolio.',
  },
  execution: {
    label: 'Execution',
    short: 'EXE',
    actor: 'Execution Agent',
    layer: 'governance',
    blurb: 'Re-checks authorization, market and asset before the broker sees it.',
  },
};

/** Convenience builder so mock proposals stay readable. */
export function stage(
  id: PipelineStageId,
  state: StageState,
  extra: Partial<Omit<PipelineStage, 'id' | 'state'>> = {},
): PipelineStage {
  const meta = STAGE_META[id];
  return {
    id,
    label: meta.label,
    actor: meta.actor,
    layer: meta.layer,
    state,
    ...extra,
  };
}

/** Human wording for each stage state, used wherever colour alone is not enough. */
export const STATE_LABEL: Record<StageState, string> = {
  IDLE: 'Idle',
  RUNNING: 'Running',
  PASSED: 'Passed',
  WATCH: 'Watch',
  REDUCED: 'Reduced',
  BLOCKED: 'Blocked',
  ERROR: 'Error',
  COMPLETE: 'Complete',
};

/** Tone drives colour; the label above always accompanies it. */
export const STATE_TONE: Record<StageState, 'ok' | 'warn' | 'danger' | 'neutral' | 'accent'> = {
  IDLE: 'neutral',
  RUNNING: 'accent',
  PASSED: 'ok',
  WATCH: 'warn',
  REDUCED: 'warn',
  BLOCKED: 'danger',
  ERROR: 'danger',
  COMPLETE: 'ok',
};
