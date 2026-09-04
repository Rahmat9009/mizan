import { NavLink } from 'react-router-dom';
import {
  BookLock,
  Cpu,
  FileCheck2,
  LayoutDashboard,
  Receipt,
  Settings2,
  ShieldAlert,
  Users,
  Wallet,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { ResponseLadder } from '@/components/domain/ResponseLevel';
import { cx } from '@/lib/format';
import { AUTONOMY_LABEL, useApp } from '@/state/app';
import { Badge } from '@/components/ui/Badge';
import { Mark } from './Mark';

interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  end?: boolean;
  /** Which half of the system this view belongs to. */
  group: 'operations' | 'governance' | 'system';
}

const NAV: NavItem[] = [
  { to: '/app', label: 'Dashboard', icon: LayoutDashboard, end: true, group: 'operations' },
  { to: '/app/proposals', label: 'Proposals', icon: FileCheck2, group: 'operations' },
  { to: '/app/portfolio', label: 'Portfolio', icon: Wallet, group: 'operations' },
  { to: '/app/risk', label: 'Risk Center', icon: ShieldAlert, group: 'governance' },
  { to: '/app/crowding', label: 'Agent crowding', icon: Users, group: 'governance' },
  { to: '/app/orders', label: 'Orders', icon: Receipt, group: 'governance' },
  { to: '/app/audit', label: 'Audit', icon: BookLock, group: 'governance' },
  { to: '/app/agents', label: 'Agents', icon: Cpu, group: 'system' },
  { to: '/app/settings', label: 'Settings', icon: Settings2, group: 'system' },
];

const GROUP_LABEL: Record<NavItem['group'], string> = {
  operations: 'Operations',
  governance: 'Governance',
  system: 'System',
};

/**
 * Primary navigation & Pinned Safety Rail.
 *
 * The safety rail is pinned to the bottom of the 244px rail rather than placed in settings:
 * paper-only environment status, autonomy mode and the graduated response ladder must remain
 * visible and reachable at all times without scrolling away. The ladder renders the current
 * posture as state, not only as a control, so an operator arriving at any screen can read
 * whether the desk is stopped and who stopped it.
 */
export function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  const { autonomy, killSwitch, executionEnabled, dryRun } = useApp();

  const groups: NavItem['group'][] = ['operations', 'governance', 'system'];

  return (
    <nav className="sidebar" aria-label="Primary">
      <div className="sidebar__brand">
        <Mark />
        <div className="sidebar__brandtext">
          <span className="sidebar__name wordmark">MIZAN</span>
          <span className="sidebar__tagline">Portfolio control layer</span>
        </div>
      </div>

      <div className="sidebar__nav">
        {groups.map((group) => (
          <div key={group} className="sidebar__group">
            <p className="u-eyebrow sidebar__grouplabel">{GROUP_LABEL[group]}</p>
            <ul>
              {NAV.filter((item) => item.group === group).map((item) => (
                <li key={item.to}>
                  <NavLink
                    to={item.to}
                    end={item.end}
                    onClick={onNavigate}
                    className={({ isActive }) => cx('sidebar__link', isActive && 'is-active')}
                  >
                    <item.icon size={15} aria-hidden="true" />
                    <span>{item.label}</span>
                  </NavLink>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>

      <div className="sidebar__rail" role="region" aria-label="Safety controls">
        <p className="u-eyebrow">Safety State</p>

        <div className="rail__row">
          <span className="rail__key">Environment</span>
          <Badge tone="paper" shape="diamond">
            PAPER ONLY
          </Badge>
        </div>

        <div className="rail__row">
          <span className="rail__key">Autonomy</span>
          <span className="rail__val u-mono">{AUTONOMY_LABEL[autonomy]}</span>
        </div>

        <div className="rail__row">
          <span className="rail__key">Execution</span>
          <span className="rail__val u-mono">
            {killSwitch ? 'Halted' : !executionEnabled ? 'Disabled' : dryRun ? 'Dry run' : 'Enabled'}
          </span>
        </div>

        <ResponseLadder />
      </div>
    </nav>
  );
}
