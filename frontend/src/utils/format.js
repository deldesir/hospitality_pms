/**
 * Locale-aware presentation helpers.
 *
 * Formatting is presentation only. Amounts, taxes and totals are always
 * calculated on the server; these helpers must never round a value that will be
 * sent back for posting.
 */
import { locale } from '@/utils/i18n'

/**
 * Intl locale tag for the active UI language.
 *
 * One entry per catalogue in `utils/i18n`; a locale without an entry formats as
 * British English, which is what every date on every board did before there was
 * a second language.
 */
const INTL_TAGS = { ar: 'ar', fr: 'fr' }

function tag() {
  return INTL_TAGS[locale.value] || 'en-GB'
}

/**
 * `2026-08-08` -> `08 Aug 2026` in the active locale.
 *
 * Date-only values are read as *this* day, not as an instant. `Intl` formats in
 * the local zone, so the obvious `new Date('2026-08-12')` — UTC midnight — prints
 * "11 Aug 2026" anywhere west of Greenwich. Every arrival and departure on every
 * board goes through this function, so that was the same one-day defect as
 * `toServerDate`, in display rather than in transport, and one the desk would
 * have seen first. See `parseServerDate`.
 */
export function formatDate(value, options = {}) {
  if (!value) return ''

  const date = value instanceof Date ? value : parseServerDate(value)
  if (!date || Number.isNaN(date.getTime())) return ''

  return new Intl.DateTimeFormat(tag(), {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
    ...options,
  }).format(date)
}

/** Date plus time, for audit trails and shift handover. */
export function formatDateTime(value) {
  return formatDate(value, {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

/** Number formatting with an explicit precision. */
export function formatNumber(value, precision = 2) {
  if (value === null || value === undefined || value === '') return ''

  return new Intl.NumberFormat(tag(), {
    minimumFractionDigits: precision,
    maximumFractionDigits: precision,
  }).format(Number(value))
}

/**
 * Currency formatting.
 *
 * The currency code always comes from the property or the document; it is never
 * assumed, because a bench may run several companies with different currencies.
 */
export function formatCurrency(value, currency, precision = 2) {
  if (value === null || value === undefined || value === '') return ''

  if (!currency) return formatNumber(value, precision)

  return new Intl.NumberFormat(tag(), {
    style: 'currency',
    currency,
    minimumFractionDigits: precision,
    maximumFractionDigits: precision,
  }).format(Number(value))
}

/**
 * `2026-08-08` for sending to the server; never locale formatted.
 *
 * A date-only string is returned as it arrived, and that is the whole point of
 * this function's shape. It used to parse everything through a `Date` first:
 *
 *     new Date('2026-08-12')        // UTC midnight, by the ECMAScript spec
 *     value.getDate()               // read back in *local* time
 *
 * Those two lines disagree anywhere west of Greenwich. At America/New_York the
 * round trip returns `2026-08-11` — a booking silently moved back a night, an
 * availability window shifted, a calendar opened on the wrong day. Qatar is
 * UTC+3 so it never reproduced on this bench, which is exactly why it survived
 * two builds: 16.7.0 found it, 16.7.1 recorded it, and 16.7.2 owns booking dates
 * so it is fixed here.
 *
 * The rule: a date-only value carries no time and no zone, so converting it
 * through an instant is never correct. It is sliced, not parsed. A `Date` object
 * is a different thing — a real instant the caller built deliberately — and its
 * local parts are read, which is what a caller who constructed local midnight
 * means. `utils/operationalDate.js` applies the same rule for the business date;
 * the two differ only in what they do with nothing (that one falls back to the
 * browser day, this one returns null).
 */
export function toServerDate(date) {
  if (!date) return null

  if (typeof date === 'string') {
    // Also narrows `2026-08-12 03:00:00` to its date, without moving it.
    const iso = /^\s*(\d{4}-\d{2}-\d{2})/.exec(date)
    if (iso) return iso[1]

    // Not an ISO date. Unsupported as a server date and not produced anywhere in
    // this app; parsed rather than refused only so an exotic caller keeps its old
    // behaviour. Anything reaching here is a bug at the call site.
    const parsed = new Date(date)

    return Number.isNaN(parsed.getTime()) ? null : localIsoDate(parsed)
  }

  if (!(date instanceof Date) || Number.isNaN(date.getTime())) return null

  return localIsoDate(date)
}

/**
 * A server date or datetime string as a `Date`, on the wall clock it names.
 *
 * `new Date('2026-08-12')` is UTC midnight while `new Date(2026, 7, 12)` is local
 * midnight — the same characters, a different instant, and only the second still
 * says "the twelfth" when formatted west of Greenwich.
 *
 * Frappe's date and datetime strings carry no zone, so they mean a wall clock,
 * not an instant, and are built from their parts. A value that *does* carry a
 * zone (`Z`, `+03:00`) is a real instant and goes to the platform parser
 * untouched — which is also the right treatment for the one field type that is
 * genuinely an instant, a `now_datetime()` timestamp.
 */
export function parseServerDate(value) {
  if (!value) return null
  if (value instanceof Date) return value

  const text = String(value).trim()

  // Zone-bearing values are instants and are not ours to reinterpret.
  if (/(?:Z|[+-]\d{2}:?\d{2})$/.test(text)) {
    const instant = new Date(text)

    return Number.isNaN(instant.getTime()) ? null : instant
  }

  const parts = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?/.exec(text)

  if (parts) {
    const [, year, month, day, hour, minute, second] = parts

    // The time is kept when the value carries one — `formatDateTime` renders
    // audit trails through this same path, and narrowing them to a day would
    // silently print every timestamp as midnight.
    return new Date(
      Number(year),
      Number(month) - 1,
      Number(day),
      Number(hour || 0),
      Number(minute || 0),
      Number(second || 0),
    )
  }

  const parsed = new Date(text)

  return Number.isNaN(parsed.getTime()) ? null : parsed
}

/** A `Date`'s own calendar day, in the zone the caller is working in. */
function localIsoDate(value) {
  const month = String(value.getMonth() + 1).padStart(2, '0')
  const day = String(value.getDate()).padStart(2, '0')

  return `${value.getFullYear()}-${month}-${day}`
}
