import { LOAD_WARN_PERCENT } from "../config";

export function Meter({ percent, load = false }: { percent: number; load?: boolean }) {
  const shown = Math.round(percent);
  const warn = load && shown >= LOAD_WARN_PERCENT;
  return (
    <span className={warn ? "meter meter-warn" : "meter"}>
      <span className="meter-track" aria-hidden="true">
        <span className="meter-fill" style={{ width: `${Math.min(Math.max(percent, 0), 100)}%` }} />
      </span>
      {shown}%
    </span>
  );
}
