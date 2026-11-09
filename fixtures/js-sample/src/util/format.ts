export function formatDate(d: Date): string {
  return d.toISOString();
}

export function parseId(raw: string): number {
  return parseInt(raw, 10);
}
