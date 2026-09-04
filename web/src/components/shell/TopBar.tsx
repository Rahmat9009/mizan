import { useState } from 'react';
import type { RefObject } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { Bell, Menu, MoonStar, Search, Sun, X } from 'lucide-react';
import { AutonomyControl } from '@/components/domain/Autonomy';
import { ResponseLevelChip } from '@/components/domain/ResponseLevel';
import { Badge } from '@/components/ui/Badge';
import { KeyHint } from '@/components/ui/Button';
import { cx, relative } from '@/lib/format';
import { useDismissable } from '@/lib/hooks';
import { useAsync } from '@/lib/hooks';
import { SESSION_ANCHOR } from '@/data/clock';
import { api } from '@/services/api';
import { useApp } from '@/state/app';

const TITLES: { match: RegExp; title: string }[] = [
  { match: /^\/app$/, title: 'Command Center' },
  { match: /^\/app\/proposals\/.+/, title: 'Proposal case file' },
  { match: /^\/app\/proposals$/, title: 'Proposals' },
  { match: /^\/app\/portfolio$/, title: 'Portfolio' },
  { match: /^\/app\/risk$/, title: 'Risk Center' },
  { match: /^\/app\/crowding$/, title: 'Agent crowding' },
  { match: /^\/app\/orders$/, title: 'Orders' },
  { match: /^\/app\/audit$/, title: 'Audit' },
  { match: /^\/app\/agents$/, title: 'Agents' },
  { match: /^\/app\/settings$/, title: 'Settings' },
];

interface TopBarProps {
  /** True only while the compact navigation drawer is open. */
  navOpen: boolean;
  onToggleNav: () => void;
  navToggleRef: RefObject<HTMLButtonElement>;
}

export function TopBar({ navOpen, onToggleNav, navToggleRef }: TopBarProps) {
  const { pathname } = useLocation();
  const { theme, toggleTheme, setPaletteOpen } = useApp();
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const panelRef = useDismissable(notificationsOpen, () => setNotificationsOpen(false));
  const notifications = useAsync(() => api.listNotifications(), []);
  const systemHealth = useAsync(() => api.getSystemHealth(), []);

  const title = TITLES.find((t) => t.match.test(pathname))?.title ?? 'Command Center';
  const items = notifications.data ?? [];
  const unread = items.filter((n) => !n.read).length;

  return (
    <header className="topbar">
      <button
        className="topbar__nav-toggle"
        ref={navToggleRef}
        onClick={onToggleNav}
        type="button"
        aria-label={navOpen ? 'Close navigation' : 'Open navigation'}
        aria-expanded={navOpen}
        aria-controls="app-navigation"
      >
        {navOpen ? <X size={17} aria-hidden="true" /> : <Menu size={17} aria-hidden="true" />}
      </button>

      <div className="topbar__identity">
        {/* Wayfinding, not the document heading. This element used to be an
            h1, which gave four routes two h1s — one here and one in their own
            PageHeader — and hid one of them from sight at narrow widths while
            leaving it in the accessibility tree. Each route now owns its single
            h1; the bar just names where you are. */}
        <p className="topbar__title">{title}</p>
        <span className="topbar__sep" aria-hidden="true" />
        <span className="topbar__clock u-mono" title="US equity session state">
          <i
            className={cx('dot', systemHealth.data?.marketOpen ? 'dot--ok' : 'dot--neutral')}
            aria-hidden="true"
          />
          {systemHealth.data?.marketOpen === null || systemHealth.data?.marketOpen === undefined
            ? 'Market state unavailable'
            : systemHealth.data.marketOpen ? 'Market open' : 'Market closed'}
        </span>
      </div>

      <div className="topbar__status">
        <Badge tone="paper" shape="diamond" title="Isolated Alpaca Paper Environment. Live broker submission disabled.">
          PAPER ONLY
        </Badge>
        <ResponseLevelChip />
        <AutonomyControl />
      </div>

      <div className="topbar__tools">
        {/* Below 1080px the label and key hint are dropped for width, leaving an
            icon on its own. The accessible name has to be carried explicitly or
            the control loses its name at exactly the sizes it is smallest. */}
        <button
          className="topbar__search"
          onClick={() => setPaletteOpen(true)}
          type="button"
          aria-label="Search or jump to a view"
        >
          <Search size={14} aria-hidden="true" />
          <span>Search or jump to…</span>
          <KeyHint keys={['⌘', 'K']} />
        </button>

        <div className="topbar__notif">
          <button
            className={cx('iconbtn', notificationsOpen && 'is-active')}
            onClick={() => setNotificationsOpen((o) => !o)}
            aria-expanded={notificationsOpen}
            aria-label={`Notifications, ${unread} unread`}
            type="button"
          >
            <Bell size={16} aria-hidden="true" />
            {unread > 0 && <span className="iconbtn__count">{unread}</span>}
          </button>
          {notificationsOpen && (
            <div className="notifpanel" ref={panelRef} role="dialog" aria-label="Notifications">
              <header className="notifpanel__head">
                <h2>Notifications</h2>
                <span className="u-dim">{unread} unread</span>
              </header>
              <ul className="notifpanel__list">
                {items.map((n) => (
                  <li key={n.id} className={cx('notif', `notif--${n.severity}`, !n.read && 'is-unread')}>
                    <Link to={n.href ?? '/app'} onClick={() => setNotificationsOpen(false)}>
                      <div className="notif__head">
                        <span className="notif__title">{n.title}</span>
                        <time dateTime={n.at} className="notif__time">
                          {relative(n.at, SESSION_ANCHOR)}
                        </time>
                      </div>
                      <p className="notif__body">{n.body}</p>
                    </Link>
                  </li>
                ))}
              </ul>
              <footer className="notifpanel__foot">
                Only decisions, blocks, fills and system faults raise a notification.
              </footer>
            </div>
          )}
        </div>

        <button
          className="iconbtn"
          onClick={toggleTheme}
          aria-label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
          type="button"
        >
          {theme === 'dark' ? <Sun size={16} aria-hidden="true" /> : <MoonStar size={16} aria-hidden="true" />}
        </button>
      </div>
    </header>
  );
}
