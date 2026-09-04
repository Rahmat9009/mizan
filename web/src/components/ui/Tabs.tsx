import { useId, useState } from 'react';
import type { ReactNode } from 'react';
import { cx } from '@/lib/format';

export interface TabItem {
  id: string;
  label: string;
  count?: number;
  content: ReactNode;
}

/** A standard tablist with roving arrow-key navigation. */
export function Tabs({ items, initial }: { items: TabItem[]; initial?: string }) {
  const [active, setActive] = useState(initial ?? items[0]?.id);
  const base = useId();

  function onKeyDown(event: React.KeyboardEvent) {
    const index = items.findIndex((i) => i.id === active);
    if (event.key === 'ArrowRight') {
      event.preventDefault();
      setActive(items[(index + 1) % items.length].id);
    } else if (event.key === 'ArrowLeft') {
      event.preventDefault();
      setActive(items[(index - 1 + items.length) % items.length].id);
    }
  }

  return (
    <div className="tabs">
      <div className="tabs__list" role="tablist" onKeyDown={onKeyDown}>
        {items.map((item) => (
          <button
            key={item.id}
            role="tab"
            id={`${base}-tab-${item.id}`}
            aria-selected={active === item.id}
            aria-controls={`${base}-panel-${item.id}`}
            tabIndex={active === item.id ? 0 : -1}
            className={cx('tabs__tab', active === item.id && 'is-active')}
            onClick={() => setActive(item.id)}
          >
            {item.label}
            {item.count !== undefined && <span className="tabs__count">{item.count}</span>}
          </button>
        ))}
      </div>
      {items.map((item) => (
        <div
          key={item.id}
          role="tabpanel"
          id={`${base}-panel-${item.id}`}
          aria-labelledby={`${base}-tab-${item.id}`}
          hidden={active !== item.id}
          className="tabs__panel"
        >
          {active === item.id && item.content}
        </div>
      ))}
    </div>
  );
}
