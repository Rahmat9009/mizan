import { motion } from 'framer-motion';
import type { ReactNode } from 'react';
import { usePrefersReducedMotion } from '@/lib/hooks';

/**
 * A single, short entrance used across the landing page.
 *
 * One reveal, reused everywhere, rather than a different flourish per section:
 * the page should feel composed, not demonstrated. Under reduced motion it
 * renders the element outright with no transition at all.
 */
export function Reveal({
  children,
  delay = 0,
  className,
  as = 'div',
}: {
  children: ReactNode;
  delay?: number;
  className?: string;
  as?: 'div' | 'section' | 'li';
}) {
  const reduced = usePrefersReducedMotion();
  const Component = motion[as];

  /* The entrance starts at zero opacity, so it is only ever attached where the
     mechanism that ends it is known to exist. Without IntersectionObserver the
     element renders outright rather than waiting for a reveal that can never
     arrive. */
  const observable = typeof IntersectionObserver !== 'undefined';

  if (reduced || !observable) {
    const Plain = as;
    return <Plain className={className}>{children}</Plain>;
  }

  return (
    <Component
      className={className}
      initial={{ opacity: 0, y: 14 }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true, margin: '-60px' }}
      transition={{ duration: 0.45, delay, ease: [0.22, 1, 0.36, 1] }}
    >
      {children}
    </Component>
  );
}
