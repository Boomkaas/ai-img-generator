import { LOAD_WARN_PERCENT } from "../config";

/** A short bar and its percentage; a `load` meter turns the warn colour when the machine is nearly full. */
export function Meter({ percent, load = false }: { percent: number; load?: boolean }) {
  const warn = load && percent >= LOAD_WARN_PERCENT;
  return (
    <span className={warn ? "meter meter-warn" : "meter"}>
      <span className="meter-track" aria-hidden="true">
        <span className="meter-fill" style={{ width: `${Math.min(Math.max(percent, 0), 100)}%` }} />
      </span>
      {Math.round(percent)}%
    </span>
  );
}
