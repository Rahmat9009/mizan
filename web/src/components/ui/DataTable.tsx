import { useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { ArrowDown, ArrowUp, ChevronsUpDown } from 'lucide-react';
import { cx } from '@/lib/format';

export interface Column<T> {
  key: string;
  header: string;
  /** Cell content. */
  render: (row: T) => ReactNode;
  /** Sort key. Omit to make the column unsortable. */
  sortValue?: (row: T) => string | number;
  align?: 'left' | 'right';
  width?: string;
  /** Numeric columns get mono-ish tabular treatment and right alignment. */
  numeric?: boolean;
  /**
   * Card layout on narrow screens: `title` and `meta` form the card header,
   * `field` rows become a label/value list, `hidden` drops out entirely.
   */
  card?: 'title' | 'meta' | 'field' | 'hidden';
  /**
   * Marks this column as the first one past the governance boundary. The table
   * draws the brass rule down its left edge, so the same device that separates
   * intelligence from authority on a page separates authorization columns from
   * broker columns inside a row.
   */
  boundary?: boolean;
}

interface DataTableProps<T> {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  onRowActivate?: (row: T) => void;
  /** Announced by screen readers as the table's purpose. */
  caption: string;
  emptyMessage?: string;
  initialSort?: { key: string; direction: 'asc' | 'desc' };
  className?: string;
}

/**
 * A dense, sortable table that degrades to stacked cards below 900px.
 *
 * Rows are activated with click, Enter or Space when `onRowActivate` is given,
 * so drill-down works without a mouse.
 */
export function DataTable<T>({
  columns,
  rows,
  rowKey,
  onRowActivate,
  caption,
  emptyMessage = 'Nothing to show.',
  initialSort,
  className,
}: DataTableProps<T>) {
  const [sort, setSort] = useState(initialSort ?? null);

  const sorted = useMemo(() => {
    if (!sort) return rows;
    const column = columns.find((c) => c.key === sort.key);
    if (!column?.sortValue) return rows;
    const factor = sort.direction === 'asc' ? 1 : -1;
    return [...rows].sort((a, b) => {
      const av = column.sortValue!(a);
      const bv = column.sortValue!(b);
      if (typeof av === 'number' && typeof bv === 'number') return (av - bv) * factor;
      return String(av).localeCompare(String(bv)) * factor;
    });
  }, [rows, columns, sort]);

  function toggleSort(key: string) {
    setSort((current) => {
      if (!current || current.key !== key) return { key, direction: 'asc' };
      if (current.direction === 'asc') return { key, direction: 'desc' };
      return null;
    });
  }

  if (rows.length === 0) {
    return <p className="table__empty">{emptyMessage}</p>;
  }

  const titleCol = columns.find((c) => c.card === 'title') ?? columns[0];
  const metaCols = columns.filter((c) => c.card === 'meta');
  const fieldCols = columns.filter((c) => c.card === 'field' || (!c.card && c !== titleCol));

  return (
    <div className={cx('tablewrap', className)}>
      {/* Wide layout */}
      <table className="table">
        <caption className="u-sr-only">{caption}</caption>
        <thead>
          <tr>
            {columns.map((col) => {
              const active = sort?.key === col.key;
              return (
                <th
                  key={col.key}
                  scope="col"
                  style={col.width ? { width: col.width } : undefined}
                  className={cx(
                    (col.numeric || col.align === 'right') && 'is-right',
                    col.boundary && 'is-boundary',
                  )}
                  aria-sort={active ? (sort!.direction === 'asc' ? 'ascending' : 'descending') : 'none'}
                >
                  {col.sortValue ? (
                    <button className="table__sort" onClick={() => toggleSort(col.key)}>
                      <span>{col.header}</span>
                      {active ? (
                        sort!.direction === 'asc' ? (
                          <ArrowUp size={12} aria-hidden="true" />
                        ) : (
                          <ArrowDown size={12} aria-hidden="true" />
                        )
                      ) : (
                        <ChevronsUpDown size={12} aria-hidden="true" className="table__sorticon" />
                      )}
                    </button>
                  ) : (
                    col.header
                  )}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {sorted.map((row) => (
            <tr
              key={rowKey(row)}
              className={cx(onRowActivate && 'table__row--activatable')}
              tabIndex={onRowActivate ? 0 : undefined}
              role={onRowActivate ? 'button' : undefined}
              onClick={onRowActivate ? () => onRowActivate(row) : undefined}
              onKeyDown={
                onRowActivate
                  ? (e) => {
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault();
                        onRowActivate(row);
                      }
                    }
                  : undefined
              }
            >
              {columns.map((col) => (
                <td
                  key={col.key}
                  className={cx(
                    (col.numeric || col.align === 'right') && 'is-right',
                    col.numeric && 'is-num',
                    col.boundary && 'is-boundary',
                  )}
                >
                  {col.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>

      {/* Narrow layout: the same rows as compact cards */}
      <ul className="cardlist">
        {sorted.map((row) => (
          <li key={rowKey(row)}>
            <div
              className={cx('cardrow', onRowActivate && 'cardrow--activatable')}
              tabIndex={onRowActivate ? 0 : undefined}
              role={onRowActivate ? 'button' : undefined}
              onClick={onRowActivate ? () => onRowActivate(row) : undefined}
              onKeyDown={
                onRowActivate
                  ? (e) => {
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault();
                        onRowActivate(row);
                      }
                    }
                  : undefined
              }
            >
              <div className="cardrow__head">
                <div className="cardrow__title">{titleCol.render(row)}</div>
                <div className="cardrow__meta">
                  {metaCols.map((c) => (
                    <span key={c.key}>{c.render(row)}</span>
                  ))}
                </div>
              </div>
              <dl className="cardrow__fields">
                {fieldCols.map((c) => (
                  <div key={c.key} className="cardrow__field">
                    <dt>{c.header}</dt>
                    <dd>{c.render(row)}</dd>
                  </div>
                ))}
              </dl>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
