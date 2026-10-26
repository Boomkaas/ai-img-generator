import type { ReactNode } from "react";

export type Tone = "accent" | "dim" | "warn";

export function Pill({ tone = "dim", children }: { tone?: Tone; children: ReactNode }) {
  return <span className={`pill pill-${tone}`}>{children}</span>;
}
