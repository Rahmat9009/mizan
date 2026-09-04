/**
 * The product mark.
 *
 * A signal enters at full width, meets the boundary, and leaves narrower. It
 * is the whole product in one glyph: nothing is stopped for its own sake, but
 * nothing passes the boundary unchanged unless policy allows it.
 */
export function Mark({ size = 22, title = 'Mizan' }: { size?: number; title?: string }) {
  return (
    <svg
      className="mark"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      role="img"
      aria-label={title}
      fill="none"
    >
      <rect x="0.75" y="0.75" width="22.5" height="22.5" rx="3.25" className="mark__plate" />
      <path d="M3.5 12h6" className="mark__in" strokeWidth="3.5" strokeLinecap="round" />
      <path d="M12 4.5v15" className="mark__gate" strokeWidth="1.5" strokeLinecap="round" />
      <path d="M14.5 12h6" className="mark__out" strokeWidth="1.75" strokeLinecap="round" />
    </svg>
  );
}
