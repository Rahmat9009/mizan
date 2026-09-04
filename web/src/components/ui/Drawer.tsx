import { useEffect } from 'react';
import type { ReactNode } from 'react';
import { X } from 'lucide-react';
import { useDismissable } from '@/lib/hooks';

interface DrawerProps {
  open: boolean;
  onClose: () => void;
  title: string;
  subtitle?: ReactNode;
  children: ReactNode;
  /** Wider drawer for lifecycle and case-file content. */
  size?: 'md' | 'lg';
}

/**
 * A right-edge drawer.
 *
 * Used wherever a row needs to expand into detail without losing the list
 * behind it: order lifecycles, audit payloads, position detail on mobile.
 */
export function Drawer({ open, onClose, title, subtitle, children, size = 'md' }: DrawerProps) {
  const ref = useDismissable(open, onClose);

  useEffect(() => {
    if (!open) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = previous;
    };
  }, [open]);

  if (!open) return null;

  return (
    <div className="drawer-root">
      <div className="drawer__scrim" onClick={onClose} aria-hidden="true" />
      <div
        className={`drawer drawer--${size}`}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        ref={ref}
      >
        <header className="drawer__head">
          <div>
            <h2 className="drawer__title">{title}</h2>
            {subtitle && <div className="drawer__subtitle">{subtitle}</div>}
          </div>
          <button className="drawer__close" onClick={onClose} aria-label="Close detail">
            <X size={16} aria-hidden="true" />
          </button>
        </header>
        <div className="drawer__body">{children}</div>
      </div>
    </div>
  );
}
