import type { ReactNode } from 'react';
import { cx } from '@/lib/format';

interface PanelProps {
  title?: ReactNode;
  /** Small caps line above the title. Use it to name the data's role. */
  eyebrow?: string;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  /** Removes body padding for tables and timelines that manage their own. */
  flush?: boolean;
  /**
   * Heading level for the panel title. A panel title is an h2 by default. A
   * route whose only visible page title is a panel title sets this to `h1` so
   * the route owns exactly one h1 without a hidden duplicate.
   */
  titleAs?: 'h1' | 'h2';
  as?: 'section' | 'div' | 'article';
}

export function Panel({
  title,
  eyebrow,
  description,
  actions,
  children,
  className,
  flush,
  as: Tag = 'section',
  titleAs: Title = 'h2',
}: PanelProps) {
  return (
    <Tag className={cx('panel', className)}>
      {(title || actions || eyebrow) && (
        <header className="panel__head">
          <div className="panel__heading">
            {eyebrow && <p className="u-eyebrow">{eyebrow}</p>}
            {title && <Title className="panel__title">{title}</Title>}
            {description && <p className="panel__desc">{description}</p>}
          </div>
          {actions && <div className="panel__actions">{actions}</div>}
        </header>
      )}
      <div className={cx('panel__body', flush && 'panel__body--flush')}>{children}</div>
    </Tag>
  );
}
