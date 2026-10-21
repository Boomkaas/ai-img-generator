import { useCallback, useEffect, useRef, useState } from "react";
import { getHistory, imageUrl } from "../api/http";
import type { TaskOut, TaskPage, TaskStatus } from "../api/types";
import { errorText } from "../errors";
import { formatClock } from "../time";

const STATUS_LABEL: Record<TaskStatus, string> = {
  queued: "Waiting",
  running: "Generating",
  done: "Done",
  failed: "Failed",
  cancelled: "Cancelled",
};

function Entry({ task, expanded, onToggle }: { task: TaskOut; expanded: boolean; onToggle: () => void }) {
  const content = (
    <>
      <span className={`dot dot-${task.status}`} aria-hidden="true" />
      <span className="entry-text">
        <span className="entry-prompt">{task.prompt}</span>
        <span className="entry-meta">
          {STATUS_LABEL[task.status]} · {task.size} px · {formatClock(task.finished_at ?? task.created_at)}
        </span>
      </span>
    </>
  );
  if (task.status !== "done") {
    return (
      <li>
        <div className="entry">{content}</div>
      </li>
    );
  }
  return (
    <li>
      <button type="button" className="entry" aria-expanded={expanded} onClick={onToggle}>
        {content}
      </button>
      {expanded && <img className="result" src={imageUrl(task.task_id)} alt={task.prompt} />}
    </li>
  );
}

export function History({ onClose }: { onClose: () => void }) {
  const [items, setItems] = useState<TaskOut[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const drawer = useRef<HTMLElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);

  const show = useCallback((page: TaskPage, more: boolean) => {
    setItems((shown) => (more ? [...shown, ...page.items] : page.items));
    setNext(page.next_before);
    setError("");
    setLoaded(true);
  }, []);
  const fail = useCallback((err: unknown) => {
    setError(errorText(err));
    setLoaded(true);
  }, []);

  useEffect(() => {
    let current = true;
    getHistory(null).then(
      (page) => current && show(page, false),
      (err) => current && fail(err),
    );
    return () => {
      current = false;
    };
  }, [show, fail]);

  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeButton.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
      if (event.key === "Tab") keepFocusInside(event);
    };
    // The page behind stays in the tab order, so Tab wraps around the drawer's own controls instead.
    const keepFocusInside = (event: KeyboardEvent) => {
      const controls = drawer.current?.querySelectorAll<HTMLElement>("button:not(:disabled), a[href]");
      const first = controls?.[0];
      const last = controls?.[controls.length - 1];
      if (!first || !last) return;
      const inside = drawer.current?.contains(document.activeElement);
      if (event.shiftKey && (document.activeElement === first || !inside)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !inside)) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      opener?.focus();
    };
  }, [onClose]);

  return (
    <>
      <div className="backdrop" onClick={onClose} />
      <aside ref={drawer} className="drawer" role="dialog" aria-modal="true" aria-labelledby="history-title">
        <header className="drawer-head">
          <h2 id="history-title">History</h2>
          <button ref={closeButton} type="button" className="link" onClick={onClose}>
            Close
          </button>
        </header>
        {loaded && !error && items.length === 0 && (
          <p className="empty">Images you generate will appear here.</p>
        )}
        <ul className="entries">
          {items.map((task) => (
            <Entry
              key={task.task_id}
              task={task}
              expanded={expanded === task.task_id}
              onToggle={() => setExpanded((id) => (id === task.task_id ? null : task.task_id))}
            />
          ))}
        </ul>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        {next && (
          <button
            type="button"
            className="link"
            disabled={loadingMore}
            onClick={() => {
              setLoadingMore(true);
              getHistory(next)
                .then((page) => show(page, true), fail)
                .finally(() => setLoadingMore(false));
            }}
          >
            Load more
          </button>
        )}
      </aside>
    </>
  );
}
