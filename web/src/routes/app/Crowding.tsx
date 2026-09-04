import { Link } from 'react-router-dom';
import { ProvenanceBadge } from '@/components/domain/ProvenanceBadge';
import { Badge } from '@/components/ui/Badge';
import { Panel } from '@/components/ui/Panel';
import { Loading, LoadError, Unavailable } from '@/components/ui/State';
import { decimalCompare, decimalPercent, decimalToRatio } from '@/lib/decimal';
import { cx } from '@/lib/format';
import { useAsync } from '@/lib/hooks';
import { api } from '@/services/api';
import type { CrowdingCluster, CrowdingReport } from '@/types/domain';

/**
 * Aggregate exposure across agent strategies.
 *
 * The failure this screen exists to show is the one no per-trade rule can see:
 * every agent inside its own limits, every deterministic check passing, and the
 * book still carrying more of a single theme than portfolio guidance allows.
 * A per-trade engine cannot detect it because a per-trade engine never looks at
 * the other agents.
 *
 * It is deliberately not a pie chart. A pie chart shows composition; the thing
 * that matters here is a theme measured against a line, and where it lands if
 * the proposals currently in review are authorized.
 */
export function Crowding() {
  const crowding = useAsync(() => api.getCrowding(), []);

  if (crowding.loading) return <Loading label="Loading aggregate exposure" />;
  if (crowding.error) return <LoadError error={crowding.error} />;
  if (!crowding.data) return null;

  const c = crowding.data;
  if (c.status === 'UNAVAILABLE') {
    return (
      <div className="page page--crowding">
        <Panel
          eyebrow="Aggregate"
          title="Agent crowding"
          titleAs="h1"
          description="Correlated exposure held across independent agent strategies."
          actions={<StatusBadge status={c.status} />}
        >
          <Unavailable reason="The backend did not record aggregate agent exposure for these decisions." />
          <p className="panel__note">
            No normal-state claim is inferred from missing aggregate data.
          </p>
        </Panel>
      </div>
    );
  }
  const allWithinLimits = c.clusters.every((cl) => cl.members.every((m) => m.withinIndividualLimits));

  /* One shared horizontal scale across all clusters, lanes, aggregates and
     projections. Geometry only — every printed percentage comes directly from
     its decimal string. */
  const everyValue = c.clusters.flatMap((cl) => [
    decimalToRatio(cl.exposure) ?? 0,
    decimalToRatio(cl.guidance) ?? 0,
    decimalToRatio(cl.projected?.exposure ?? '0') ?? 0,
    ...cl.members.map((m) => decimalToRatio(m.weight) ?? 0),
  ]);
  const sharedScale = Math.max(...everyValue, 0.0001) * 1.15 || 1;

  return (
    <div className="page page--crowding">
      <Panel
        eyebrow="Aggregate"
        title="Agent crowding"
        titleAs="h1"
        description="Correlated exposure held across independent agent strategies."
        actions={<StatusBadge status={c.status} />}
      >
        <p className="crowd__lede">
          {c.agentsCorrelated} of {c.agentsTotal} agent strategies hold correlated same-direction exposure.
        </p>

        {allWithinLimits && (
          <p className="crowd__claim">
            <strong>Each agent is within its individual limits.</strong> No deterministic per-trade check fails on any
            position below. The aggregate is a portfolio-level fact, and it is the one a per-trade engine cannot see.
          </p>
        )}

        <p className="panel__note">
          <ProvenanceBadge value={c.provenance} size="xs" /> Weights are shares of account equity and match the{' '}
          <Link to="/app/portfolio">portfolio positions</Link> exactly.
        </p>
      </Panel>

      {/* The unified scale header: establishes a single common horizontal measurement axis */}
      <div className="crowdscale" aria-hidden="true">
        <div className="crowdscale__head">
          <span className="crowdscale__headkey">Agent · strategy</span>
          <span className="crowdscale__axis">
            <span className="crowdscale__axistick">0%</span>
            <span className="crowdscale__axistick">Shared axis · max {decimalPercent(String(sharedScale), 0)}</span>
            <span className="crowdscale__axistick">{decimalPercent(String(sharedScale), 0)}</span>
          </span>
          <span className="crowdscale__headmeta">Symbol</span>
          <span className="crowdscale__headval">Weight</span>
          <span />
        </div>
      </div>

      {c.clusters.map((cluster) => (
        <ClusterPanel key={cluster.id} cluster={cluster} sharedScale={sharedScale} />
      ))}

      <div className="crowd__lower">
        <Panel eyebrow="Independence" title="Concentration of judgement">
          <ul className="crowd__facts">
            {c.modelConcentration && (
              <li className="crowd__fact">
                <span className="crowd__factshare u-mono">{decimalPercent(c.modelConcentration.share, 0)}</span>
                <div>
                  <p className="crowd__factlabel">{c.modelConcentration.label}</p>
                  <p className="crowd__factdetail">{c.modelConcentration.detail}</p>
                </div>
              </li>
            )}
            {c.signalConcentration && (
              <li className="crowd__fact">
                <span className="crowd__factshare u-mono">{decimalPercent(c.signalConcentration.share, 0)}</span>
                <div>
                  <p className="crowd__factlabel">{c.signalConcentration.label}</p>
                  <p className="crowd__factdetail">{c.signalConcentration.detail}</p>
                </div>
              </li>
            )}
          </ul>
          <p className="panel__note">
            Diversification across agents is not diversification if the agents share a model or a signal. A fault in
            either is common to all of them.
          </p>
        </Panel>

        <Panel eyebrow="Liquidity" title="Simultaneous exit">
          {c.unwindDays !== null ? (
            <p className="crowd__unwind">
              <strong className="u-mono">{c.unwindDays} days</strong> to unwind the correlated book at normal volume.
            </p>
          ) : (
            <>
              <p className="crowd__unwind">
                <Unavailable reason="No market-volume feed is connected to this backend." />
              </p>
              <p className="panel__note">
                Time-to-unwind needs a volume feed. Nothing here supplies one, so the figure is not estimated. It reads
                Unavailable rather than being invented.
              </p>
            </>
          )}
        </Panel>
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- a cluster */

function ClusterPanel({ cluster, sharedScale }: { cluster: CrowdingCluster; sharedScale: number }) {
  // Geometry only. The percentages beside each bar come from the decimal
  // strings; these floats size the rectangles on the shared scale.
  const pct = (d: string | null | undefined) => ((decimalToRatio(d ?? '0') ?? 0) / sharedScale) * 100;
  const guidanceLeft = pct(cluster.guidance);
  const projectedOver =
    cluster.projected !== null && decimalCompare(cluster.projected.exposure, cluster.guidance) > 0;

  return (
    <Panel
      eyebrow={`Cluster · Guidance ${decimalPercent(cluster.guidance)}`}
      title={cluster.label}
      actions={
        <div className="u-flex-row u-gap-2 u-items-center">
          <Badge tone={cluster.breached ? 'warn' : 'ok'} glyph={cluster.breached ? '▼' : '●'}>
            {cluster.breached ? 'Above guidance' : 'Within guidance'}
          </Badge>
        </div>
      }
    >
      <ul className="crowd__members">
        {cluster.members.map((m) => (
          <li key={`${m.agentId}-${m.symbol}`} className="crowd__member">
            <span className="crowd__agent">{m.agentId}</span>
            <span className="crowd__track">
              <span
                className="crowd__bar"
                style={{ width: `${pct(m.weight)}%` }}
              />
              <span
                className="crowd__guide"
                style={{ left: `${guidanceLeft}%` }}
                aria-hidden="true"
                title={`Guidance: ${decimalPercent(cluster.guidance)}`}
              />
            </span>
            <span className="crowd__symbol u-mono">{m.symbol}</span>
            <span className="crowd__weight u-mono">{decimalPercent(m.weight)}</span>
            <span className={cx('crowd__mark', m.withinIndividualLimits ? 'is-ok' : 'is-bad')}>
              <span aria-hidden="true">{m.withinIndividualLimits ? '✓' : '✕'}</span>
              <span className="u-sr-only">
                {m.withinIndividualLimits ? 'within its own limits' : 'outside its own limits'}
              </span>
            </span>
          </li>
        ))}

        <li className="crowd__member crowd__member--total">
          <span className="crowd__agent">Aggregate</span>
          <span className="crowd__track">
            <span
              className={cx('crowd__bar', cluster.breached && 'is-over')}
              style={{ width: `${pct(cluster.exposure)}%` }}
            />
            {cluster.breached && (
              <span
                className="crowd__excess"
                style={{
                  left: `${guidanceLeft}%`,
                  width: `${Math.max(pct(cluster.exposure) - guidanceLeft, 0)}%`,
                }}
                aria-hidden="true"
              />
            )}
            <span
              className="crowd__guide"
              style={{ left: `${guidanceLeft}%` }}
              aria-hidden="true"
              title={`Guidance: ${decimalPercent(cluster.guidance)}`}
            />
          </span>
          <span className="crowd__symbol u-dim">{cluster.members.length} positions</span>
          <span className="crowd__weight u-mono">{decimalPercent(cluster.exposure)}</span>
          <span className={cx('crowd__mark', cluster.breached ? 'is-bad' : 'is-ok')}>
            <span aria-hidden="true">{cluster.breached ? '✕' : '✓'}</span>
            <span className="u-sr-only">{cluster.breached ? 'above guidance' : 'within guidance'}</span>
          </span>
        </li>

        {cluster.projected && (
          <li className="crowd__member crowd__member--projected">
            <span className="crowd__agent">Projected</span>
            <span className="crowd__track">
              <span
                className={cx(
                  'crowd__bar',
                  'is-projected',
                  projectedOver && 'is-over',
                )}
                style={{ width: `${pct(cluster.projected.exposure)}%` }}
              />
              {projectedOver && (
                <span
                  className="crowd__excess"
                  style={{
                    left: `${guidanceLeft}%`,
                    width: `${Math.max(pct(cluster.projected.exposure) - guidanceLeft, 0)}%`,
                  }}
                  aria-hidden="true"
                />
              )}
              <span
                className="crowd__guide"
                style={{ left: `${guidanceLeft}%` }}
                aria-hidden="true"
                title={`Guidance: ${decimalPercent(cluster.guidance)}`}
              />
            </span>
            <span className="crowd__symbol u-dim">in review</span>
            <span className="crowd__weight u-mono">{decimalPercent(cluster.projected.exposure)}</span>
            <span
              className={cx(
                'crowd__mark',
                projectedOver ? 'is-bad' : 'is-ok',
              )}
            >
              <span aria-hidden="true">
                {projectedOver ? '✕' : '✓'}
              </span>
              <span className="u-sr-only">
                {projectedOver
                  ? 'projected above guidance'
                  : 'projected within guidance'}
              </span>
            </span>
          </li>
        )}
      </ul>

      <p className="crowd__guidance">
        Portfolio guidance <span className="u-mono">{decimalPercent(cluster.guidance)}</span>
        {cluster.projected && <> · Projected {cluster.projected.label}</>}
        {/* The screen diagnosed a breach and then offered nothing to do about
            it. The proposal that would cause it is still in review, so the page
            hands the reader the way to it rather than a dead end. Nothing here
            invents an action the backend cannot perform. */}
        {cluster.projected && decimalCompare(cluster.projected.exposure, cluster.guidance) > 0 && (
          <>
            {' · '}
            <Link className="crowd__lever" to="/app/proposals">
              Review the proposals in flight
            </Link>
          </>
        )}
      </p>
    </Panel>
  );
}

/**
 * Aggregate state, in words the response ladder does not already own.
 *
 * "ELEVATED" is Level 1 of the graduated response ladder, and the safety rail
 * renders it as a posture on every screen. Using the same word here for an
 * unrelated severity put two meanings on one term inside a safety vocabulary —
 * a desk reading "ELEVATED" could not tell whether the system had escalated.
 */
function StatusBadge({ status }: { status: CrowdingReport['status'] }) {
  if (status === 'UNAVAILABLE') return <Badge tone="neutral">UNAVAILABLE</Badge>;
  if (status === 'BREACH') return <Badge tone="danger" glyph="✕">ABOVE GUIDANCE</Badge>;
  if (status === 'ELEVATED') return <Badge tone="warn" glyph="▼">APPROACHING GUIDANCE</Badge>;
  return <Badge tone="ok" glyph="●">WITHIN GUIDANCE</Badge>;
}
