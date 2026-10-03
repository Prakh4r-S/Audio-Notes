import { STATUS_LABEL, type Status } from "@/lib/api";

export function StatusBadge({ status }: { status: Status }) {
  return <span className={`badge ${status}`}>{STATUS_LABEL[status]}</span>;
}
