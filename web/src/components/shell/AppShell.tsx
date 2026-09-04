import { useEffect, useRef, useState } from 'react';
import { Outlet, useLocation } from 'react-router-dom';
import { ResponseBanner } from '@/components/domain/ResponseLevel';
import { cx } from '@/lib/format';
import { useMediaQuery } from '@/lib/hooks';
import { CommandPalette } from './CommandPalette';
import { Sidebar } from './Sidebar';
import { TopBar } from './TopBar';

/**
 * The application shell.
 *
 * Desktop keeps the sidebar permanently visible; below 1080px it becomes an
 * overlay so the content column keeps its width. The safety rail travels with
 * the sidebar, and the paper-only badge stays in the top bar at every size, so
 * the environment is never off-screen.
 *
 * A closed overlay drawer is translated off-screen, which hides it from sight
 * but not from the keyboard. `inert` takes its controls out of the tab order
 * and the accessibility tree for as long as it is closed — `aria-hidden` alone
 * was wrong here, because it leaves focusable descendants reachable while
 * claiming they are not there. The stylesheet carries the same guarantee
 * through `visibility`, timed to the slide so the drawer is still visible while
 * it animates out.
 */
export function AppShell() {
  const [navOpen, setNavOpen] = useState(false);
  const compact = useMediaQuery('(max-width: 1080px)');
  const { pathname } = useLocation();

  const sideRef = useRef<HTMLElement>(null);
  const navToggleRef = useRef<HTMLButtonElement>(null);
  const wasOpen = useRef(false);

  useEffect(() => setNavOpen(false), [pathname]);
  useEffect(() => {
    if (!compact) setNavOpen(false);
  }, [compact]);

  const navClosed = compact && !navOpen;

  useEffect(() => {
    const el = sideRef.current;
    if (el) el.inert = navClosed;
  }, [navClosed]);

  /* Focus follows the drawer: into it on open, back to the control that opened
     it on close. Without the second half, dismissing the drawer would drop
     focus on the document body. */
  useEffect(() => {
    if (!compact) {
      wasOpen.current = false;
      return;
    }
    if (navOpen) {
      const first = sideRef.current?.querySelector<HTMLElement>('a, button');
      first?.focus();
    } else if (wasOpen.current) {
      const active = document.activeElement;
      if (!active || active === document.body || sideRef.current?.contains(active)) {
        navToggleRef.current?.focus();
      }
    }
    wasOpen.current = navOpen;
  }, [navOpen, compact]);

  useEffect(() => {
    if (!compact || !navOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setNavOpen(false);
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [compact, navOpen]);

  return (
    <div className={cx('shell', navOpen && 'shell--navopen')}>
      <a className="u-skip-link" href="#main">
        Skip to content
      </a>

      <aside className="shell__side" ref={sideRef} id="app-navigation">
        <Sidebar onNavigate={() => setNavOpen(false)} />
      </aside>

      {compact && navOpen && <div className="shell__scrim" onClick={() => setNavOpen(false)} aria-hidden="true" />}

      <div className="shell__main">
        <TopBar
          navOpen={compact && navOpen}
          onToggleNav={() => setNavOpen((open) => !open)}
          navToggleRef={navToggleRef}
        />
        <main id="main" className="shell__content" tabIndex={-1}>
          <ResponseBanner />
          <Outlet />
        </main>
      </div>

      <CommandPalette />
    </div>
  );
}
