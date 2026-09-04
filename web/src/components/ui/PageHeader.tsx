import type { ReactNode } from 'react';

/** Standard page introduction: what this view is for, and its page-level actions. */
export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow?: string;
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="pagehead">
      <div className="pagehead__text">
        {eyebrow && <p className="u-eyebrow">{eyebrow}</p>}
        <h1 className="pagehead__title">{title}</h1>
        {description && <p className="pagehead__desc">{description}</p>}
      </div>
      {actions && <div className="pagehead__actions">{actions}</div>}
    </header>
  );
}
