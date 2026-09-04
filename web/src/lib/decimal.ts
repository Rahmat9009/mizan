/**
 * Decimal-string arithmetic and formatting.
 *
 * MIZAN-UX-SPEC §7: money and quantity are decimal strings end to end. The
 * frontend must never parse them into JavaScript floats, because `0.1 + 0.2`
 * will eventually appear in a compliance screenshot.
 *
 * Everything here operates on the string representation. The only place a
 * `Decimal` becomes a `number` is `decimalToRatio`, which exists solely to
 * compute a bar width in pixels — a geometric quantity that is never displayed
 * and never round-trips back into a figure a user reads.
 */

/** A fixed-point decimal carried as a string, exactly as the backend emits it. */
export type Decimal = string;

interface Parts {
  negative: boolean;
  int: string;
  frac: string;
}

function parse(value: Decimal): Parts | null {
  const match = /^([+-]?)(\d*)(?:\.(\d*))?$/.exec(value.trim());
  if (!match || (match[2] === '' && (match[3] ?? '') === '')) return null;
  return {
    negative: match[1] === '-',
    int: match[2] === '' ? '0' : match[2],
    frac: match[3] ?? '',
  };
}

/** Rounds a digit string half-up at `digits` places, carrying into the integer part. */
function round(parts: Parts, digits: number): Parts {
  if (parts.frac.length <= digits) {
    return { ...parts, frac: parts.frac.padEnd(digits, '0') };
  }
  const keep = parts.frac.slice(0, digits);
  const roundUp = Number(parts.frac[digits]) >= 5;
  if (!roundUp) return { ...parts, frac: keep };

  // Increment the kept digits as one integer string, then carry into `int`.
  const bumped = incrementDigits(parts.int + keep);
  const intLength = bumped.length - digits;
  return {
    negative: parts.negative,
    int: bumped.slice(0, intLength),
    frac: bumped.slice(intLength),
  };
}

function incrementDigits(digits: string): string {
  const out = digits.split('');
  for (let i = out.length - 1; i >= 0; i -= 1) {
    if (out[i] !== '9') {
      out[i] = String(Number(out[i]) + 1);
      return out.join('');
    }
    out[i] = '0';
  }
  return `1${out.join('')}`;
}

/** Moves the decimal point `places` to the right. `shift('0.187', 2) === '18.7'`. */
function shift(parts: Parts, places: number): Parts {
  let digits = parts.int + parts.frac;
  let point = parts.int.length + places;
  while (point > digits.length) digits += '0';
  while (point < 0) {
    digits = `0${digits}`;
    point += 1;
  }
  return {
    negative: parts.negative,
    int: digits.slice(0, point).replace(/^0+(?=\d)/, '') || '0',
    frac: digits.slice(point),
  };
}

function group(int: string): string {
  return int.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

function render(parts: Parts, { grouped = false } = {}): string {
  const int = grouped ? group(parts.int) : parts.int;
  const body = parts.frac ? `${int}.${parts.frac}` : int;
  // A rounded-away sign ("-0.00") would read as a loss that did not happen.
  const isZero = /^0*$/.test(parts.int) && /^0*$/.test(parts.frac);
  return parts.negative && !isZero ? `−${body}` : body;
}

/** `'4200.00'` → `'$4,200.00'`. Returns `null` for an unparseable input. */
export function decimalMoney(value: Decimal | null | undefined, digits = 2): string | null {
  if (value === null || value === undefined) return null;
  const parts = parse(value);
  if (!parts) return null;
  const rounded = round(parts, digits);
  const body = render(rounded, { grouped: true });
  return body.startsWith('−') ? `−$${body.slice(1)}` : `$${body}`;
}

/** `'4820000.00'` → `'$4.82M'`. Used only where the exact figure is available elsewhere. */
export function decimalMoneyCompact(value: Decimal | null | undefined): string | null {
  if (value === null || value === undefined) return null;
  const parts = parse(value);
  if (!parts) return null;
  const magnitude = parts.int.replace(/^0+(?=\d)/, '').length;
  const scale = magnitude > 9 ? 9 : magnitude > 6 ? 6 : magnitude > 3 ? 3 : 0;
  const suffix = scale === 9 ? 'B' : scale === 6 ? 'M' : scale === 3 ? 'K' : '';
  const scaled = round(shift(parts, -scale), scale === 0 ? 0 : 2);
  const body = render(scaled, { grouped: true });
  return `${body.startsWith('−') ? `−$${body.slice(1)}` : `$${body}`}${suffix}`;
}

/** `'0.187'` → `'18.7%'`. The ratio is shifted, never multiplied. */
export function decimalPercent(value: Decimal | null | undefined, digits = 1): string | null {
  if (value === null || value === undefined) return null;
  const parts = parse(value);
  if (!parts) return null;
  return `${render(round(shift(parts, 2), digits))}%`;
}

/** `'0.187'` → `'0.19'`. For scores and scalars that are not percentages. */
export function decimalFixed(value: Decimal | null | undefined, digits = 2): string | null {
  if (value === null || value === undefined) return null;
  const parts = parse(value);
  if (!parts) return null;
  return render(round(parts, digits), { grouped: true });
}

/** Compares two decimals without converting either to a float. -1 | 0 | 1. */
export function decimalCompare(a: Decimal, b: Decimal): number {
  const pa = parse(a);
  const pb = parse(b);
  if (!pa || !pb) return 0;
  if (pa.negative !== pb.negative) return pa.negative ? -1 : 1;
  const width = Math.max(pa.frac.length, pb.frac.length);
  const da = pa.int.padStart(40, '0') + pa.frac.padEnd(width, '0');
  const db = pb.int.padStart(40, '0') + pb.frac.padEnd(width, '0');
  const cmp = da === db ? 0 : da < db ? -1 : 1;
  return pa.negative ? -cmp : cmp;
}

/**
 * A float, for geometry only.
 *
 * Bar widths and meter fills need a number. The value returned here is used to
 * size a rectangle and nothing else — every figure printed beside that
 * rectangle comes from the decimal string itself.
 */
export function decimalToRatio(value: Decimal | null | undefined): number | null {
  if (value === null || value === undefined) return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}
