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
      <aside className="drawer" role="dialog" aria-modal="true" aria-labelledby="history-title">
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
            onClick={() => getHistory(next).then((page) => show(page, true), fail)}
          >
            Load more
          </button>
        )}
      </aside>
    </>
  );
}
