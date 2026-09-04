import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { CornerDownLeft } from 'lucide-react';
import { cx } from '@/lib/format';
import { useDismissable } from '@/lib/hooks';
import { useAsync } from '@/lib/hooks';
import { api } from '@/services/api';
import { useApp } from '@/state/app';

interface Command {
  id: string;
  label: string;
  hint: string;
  group: 'Navigate' | 'Proposals' | 'Orders' | 'Controls';
  run: () => void;
}

/**
 * The command palette.
 *
 * Opens on ⌘K / Ctrl-K. It searches the things an operator actually addresses
 * by name — proposals and client order IDs — as well as the views, so a case
 * file is one keystroke away from anywhere.
 */
export function CommandPalette() {
  const { paletteOpen, setPaletteOpen, toggleTheme, setKillSwitch, killSwitch } = useApp();
  const navigate = useNavigate();
  const [query, setQuery] = useState('');
  const [cursor, setCursor] = useState(0);
  const ref = useDismissable(paletteOpen, () => setPaletteOpen(false));
  const proposals = useAsync(() => api.listProposals(), []);
  const orders = useAsync(() => api.listOrders(), []);

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        setPaletteOpen(!paletteOpen);
      }
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [paletteOpen, setPaletteOpen]);

  useEffect(() => {
    if (paletteOpen) {
      setQuery('');
      setCursor(0);
    }
  }, [paletteOpen]);

  const commands = useMemo<Command[]>(() => {
    const go = (to: string) => () => {
      navigate(to);
      setPaletteOpen(false);
    };
    return [
      { id: 'nav-dash', label: 'Dashboard', hint: 'Command Center', group: 'Navigate', run: go('/app') },
      { id: 'nav-prop', label: 'Proposals', hint: 'All evaluated proposals', group: 'Navigate', run: go('/app/proposals') },
      { id: 'nav-port', label: 'Portfolio', hint: 'Positions and exposure', group: 'Navigate', run: go('/app/portfolio') },
      { id: 'nav-risk', label: 'Risk Center', hint: 'Policy, utilisation, interventions', group: 'Navigate', run: go('/app/risk') },
      { id: 'nav-ord', label: 'Orders', hint: 'Lifecycle and fills', group: 'Navigate', run: go('/app/orders') },
      { id: 'nav-aud', label: 'Audit', hint: 'Forensic timeline and replay', group: 'Navigate', run: go('/app/audit') },
      { id: 'nav-agt', label: 'Agents', hint: 'Operations center', group: 'Navigate', run: go('/app/agents') },
      { id: 'nav-set', label: 'Settings', hint: 'Autonomy, policy, connections', group: 'Navigate', run: go('/app/settings') },
      { id: 'ctl-theme', label: 'Toggle theme', hint: 'Dark and light', group: 'Controls', run: () => { toggleTheme(); setPaletteOpen(false); } },
      {
        id: 'ctl-kill',
        label: killSwitch ? 'Turn the kill switch off' : 'Turn the kill switch on',
        hint: killSwitch ? 'Allow submissions again' : 'Refuse every broker submission',
        group: 'Controls',
        run: () => { setKillSwitch(!killSwitch); setPaletteOpen(false); },
      },
      ...(proposals.data ?? []).map<Command>((p) => ({
        id: `prop-${p.proposalId}`,
        label: p.instrument.type === 'equity' ? `${p.instrument.symbol} · ${p.instrument.side}` : `${p.instrument.underlying} · spread`,
        hint: p.proposalId,
        group: 'Proposals',
        run: go(`/app/proposals/${p.proposalId}`),
      })),
      ...(orders.data ?? []).map<Command>((o) => ({
        id: `ord-${o.clientOrderId}`,
        label: `${o.symbol} · ${o.lifecycle.replace('_', ' ').toLowerCase()}`,
        hint: o.clientOrderId,
        group: 'Orders',
        run: go('/app/orders'),
      })),
    ];
  }, [navigate, setPaletteOpen, toggleTheme, killSwitch, setKillSwitch, proposals.data, orders.data]);

  const results = useMemo(() => {
    if (!query.trim()) return commands.slice(0, 10);
    const needle = query.toLowerCase();
    return commands.filter((c) => `${c.label} ${c.hint} ${c.group}`.toLowerCase().includes(needle)).slice(0, 12);
  }, [commands, query]);

  useEffect(() => setCursor(0), [query]);

  if (!paletteOpen) return null;

  const grouped = results.reduce<Record<string, Command[]>>((acc, c) => {
    (acc[c.group] ??= []).push(c);
    return acc;
  }, {});

  let runningIndex = -1;

  return (
    <div className="palette-root">
      <div className="palette__scrim" onClick={() => setPaletteOpen(false)} aria-hidden="true" />
      <div className="palette" role="dialog" aria-modal="true" aria-label="Command palette" ref={ref}>
        <input
          className="palette__input"
          autoFocus
          value={query}
          placeholder="Search proposals, orders and views"
          aria-label="Search proposals, orders and views"
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'ArrowDown') {
              e.preventDefault();
              setCursor((c) => Math.min(c + 1, results.length - 1));
            } else if (e.key === 'ArrowUp') {
              e.preventDefault();
              setCursor((c) => Math.max(c - 1, 0));
            } else if (e.key === 'Enter') {
              e.preventDefault();
              results[cursor]?.run();
            }
          }}
        />
        <div className="palette__results">
          {results.length === 0 && <p className="palette__empty">Nothing matches “{query}”.</p>}
          {Object.entries(grouped).map(([group, items]) => (
            <div key={group} className="palette__group">
              <p className="u-eyebrow">{group}</p>
              <ul>
                {items.map((c) => {
                  runningIndex += 1;
                  const index = runningIndex;
                  return (
                    <li key={c.id}>
                      <button
                        className={cx('palette__item', index === cursor && 'is-cursor')}
                        onMouseEnter={() => setCursor(index)}
                        onClick={c.run}
                      >
                        <span className="palette__label">{c.label}</span>
                        <span className="palette__hint u-mono">{c.hint}</span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            </div>
          ))}
        </div>
        <footer className="palette__foot">
          <span>
            <CornerDownLeft size={11} aria-hidden="true" /> open
          </span>
          <span>↑↓ move</span>
          <span>esc close</span>
        </footer>
      </div>
    </div>
  );
}
