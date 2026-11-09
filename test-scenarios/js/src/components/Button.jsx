import { formatDate } from "../util/format";

export function Button({ label }) {
  const stamp = formatDate(new Date());
  return <button data-stamp={stamp}>{label}</button>;
}

export const Secondary = ({ label }) => <span>{label}</span>;
