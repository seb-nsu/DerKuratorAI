// Number formats of the Overview and Data quality pages – the same styles as the NiceGUI pages ("€9.92 M",
// "€420,000", "85 %"). Display only: every figure itself comes from the API.
const grouped = (value: number, digits: number) =>
  value.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
const sign = (value: number) => (value < 0 ? "−" : "");

export const eur = (value: number | null | undefined) =>
  value == null ? "–" : `${sign(value)}€${grouped(Math.abs(value), 0)}`;

export const eurM = (value: number | null | undefined, digits = 2) =>
  value == null ? "–" : `${sign(value)}€${grouped(Math.abs(value) / 1e6, digits)} M`;

export const pct = (value: number | null | undefined, digits = 0) => (value == null ? "–" : `${grouped(value, digits)} %`);

/** A limit as the business rules write it: 100, 110.5. */
export const short = (value: number) => String(Number(value.toPrecision(6)));

