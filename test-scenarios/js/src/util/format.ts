export function formatDate(d: Date): string {
  return d.toISOString();
}

export function parseId(raw: string): number {
  return parseInt(raw, 10);
}

export class DateFormatter {
  private readonly locale: string;
  private readonly options: Intl.DateTimeFormatOptions;

  constructor(locale: string, options: Intl.DateTimeFormatOptions) {
    this.locale = locale;
    this.options = options;
  }

  format(d: Date): string {
    return new Intl.DateTimeFormat(this.locale, this.options).format(d);
  }
}
