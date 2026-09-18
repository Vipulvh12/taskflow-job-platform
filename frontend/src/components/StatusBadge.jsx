// The six job states, plus the two an attempt row can carry. RETRYING is amber
// rather than red on purpose: the job hasn't failed, it's still moving.
const STYLES = {
  QUEUED: { bg: "#e2e8f0", fg: "#334155" },
  RUNNING: { bg: "#bfdbfe", fg: "#1e3a8a" },
  RETRYING: { bg: "#fde68a", fg: "#78350f" },
  SUCCESS: { bg: "#bbf7d0", fg: "#166534" },
  FAILED: { bg: "#fecaca", fg: "#7f1d1d" },
  DEAD: { bg: "#fecaca", fg: "#7f1d1d" },
};

export function StatusBadge({ status }) {
  const style = STYLES[status] ?? STYLES.QUEUED;
  return (
    <span
      className="badge"
      style={{ background: style.bg, color: style.fg }}
      title={status}
    >
      {status}
    </span>
  );
}
