import type { ButtonHTMLAttributes, ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { cx } from '@/lib/format';

type Variant = 'primary' | 'secondary' | 'ghost' | 'danger';
type Size = 'sm' | 'md' | 'lg';

interface CommonProps {
  variant?: Variant;
  size?: Size;
  children: ReactNode;
  className?: string;
  iconLeft?: ReactNode;
  iconRight?: ReactNode;
}

export function Button({
  variant = 'secondary',
  size = 'md',
  children,
  className,
  iconLeft,
  iconRight,
  ...rest
}: CommonProps & ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button type="button" className={cx('btn', `btn--${variant}`, `btn--${size}`, className)} {...rest}>
      {iconLeft}
      <span>{children}</span>
      {iconRight}
    </button>
  );
}

export function ButtonLink({
  to,
  variant = 'secondary',
  size = 'md',
  children,
  className,
  iconLeft,
  iconRight,
}: CommonProps & { to: string }) {
  return (
    <Link to={to} className={cx('btn', `btn--${variant}`, `btn--${size}`, className)}>
      {iconLeft}
      <span>{children}</span>
      {iconRight}
    </Link>
  );
}

/** A keyboard hint rendered inline, e.g. the ⌘K affordance in the top bar. */
export function KeyHint({ keys }: { keys: string[] }) {
  return (
    <span className="keyhint" aria-hidden="true">
      {keys.map((k) => (
        <kbd key={k}>{k}</kbd>
      ))}
    </span>
  );
}
