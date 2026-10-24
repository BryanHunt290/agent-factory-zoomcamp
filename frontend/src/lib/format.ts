// Time and text formatting. Pure functions: easy to test, no React.

export interface FormatOptions {
  locale?: string;
  timeZone?: string;
}

function toDate(value: string | number | Date): Date {
  return value instanceof Date ? value : new Date(value);
}

/** "10:42:17 AM" in the reader's time zone: for "showing data from …" and similar. */
export function formatClock(value: string | number | Date, options: FormatOptions = {}): string {
  return toDate(value).toLocaleTimeString(options.locale, {
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
    timeZone: options.timeZone,
  });
}

/** "Oct 5, 10:42 AM" in the reader's time zone. */
export function formatDateTime(value: string | number | Date, options: FormatOptions = {}): string {
  return toDate(value).toLocaleString(options.locale, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZone: options.timeZone,
  });
}

/** "45 s", "12 min", "3 h 5 min", "2 days". */
export function formatDuration(milliseconds: number): string {
  const seconds = Math.max(0, Math.round(Math.abs(milliseconds) / 1000));
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (hours < 48) return rest && hours < 10 ? `${hours} h ${rest} min` : `${hours} h`;
  const days = Math.round(hours / 24);
  return `${days} days`;
}

/** "just now", "5 min ago", "in 11 h 58 min". */
export function formatRelative(value: string | number | Date, now: number): string {
  const delta = toDate(value).getTime() - now;
  if (Math.abs(delta) < 45_000) return delta <= 0 ? "just now" : "in under a minute";
  const span = formatDuration(delta);
  return delta < 0 ? `${span} ago` : `in ${span}`;
}

/** A long identifier shortened for narrow screens; the full value is shown in details. */
export function shortId(id: string, keep = 6): string {
  const dash = id.indexOf("-");
  if (dash < 0 || id.length <= dash + 1 + keep + 1) return id;
  return `${id.slice(0, dash + 1 + keep)}…`;
}

/** Plain text for one action parameter value: strings as they are, anything else as JSON. */
export function parameterText(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}
