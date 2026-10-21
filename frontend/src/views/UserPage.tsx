import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { cancelTask, getActiveTask, getTask } from "../api/http";
import { openSocket } from "../api/socket";
import type { FinishedStatus, TaskOut, User } from "../api/types";
import { errorText } from "../errors";
import { initialState, isActive, reducer } from "../state";
import { StatusLine, TaskForm, TaskView } from "./Generator";
import { History } from "./History";

export function UserPage({ user, onLogOut }: { user: User; onLogOut: () => void }) {
  const [state, dispatch] = useReducer(reducer, initialState);
  const [historyOpen, setHistoryOpen] = useState(false);
  const stateRef = useRef(state);
  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  // Fetch a task that has ended, so the done or failed view has everything it shows.
  const settle = useCallback(async (id: string, status?: FinishedStatus) => {
    try {
      dispatch({ type: "settled", task: await getTask(id) });
    } catch {
      const current = stateRef.current.task;
      if (status && current?.task_id === id) dispatch({ type: "settled", task: { ...current, status } });
    }
  }, []);

  useEffect(() => {
    let stopped = false;
    let close: (() => void) | undefined;
    // Restores the job view after a reload; the socket's snapshot then keeps it current.
    void getActiveTask()
      .then((task) => {
        if (!stopped) dispatch({ type: "restored", task });
      })
      .catch(() => undefined)
      .finally(() => {
        if (stopped) return;
        close = openSocket((message) => {
          const current = stateRef.current.task;
          switch (message.type) {
            case "snapshot":
              dispatch(message);
              if (!message.task && current && isActive(current)) void settle(current.task_id);
              break;
            case "system":
            case "task":
              dispatch(message);
              break;
            case "task_final":
              dispatch(message);
              if (current?.task_id === message.id && message.status !== "cancelled") {
                void settle(message.id, message.status);
              }
              break;
          }
        });
      });
    return () => {
      stopped = true;
      close?.();
    };
  }, [settle]);

  // Returns the sentence to show when the cancel itself failed.
  const onCancel = useCallback(
    async (task: TaskOut) => {
      try {
        const { status } = await cancelTask(task.task_id);
        // It finished on its own just before: show how it ended instead.
        if (status === "cancelled") dispatch({ type: "cancelled", id: task.task_id });
        else void settle(task.task_id);
        return "";
      } catch (error) {
        return errorText(error);
      }
    },
    [settle],
  );
  const closeHistory = useCallback(() => setHistoryOpen(false), []);

  return (
    <main className="column">
      <h1>Generate</h1>
      {state.system && <StatusLine system={state.system} />}
      {state.task ? (
        <TaskView task={state.task} dispatch={dispatch} onCancel={onCancel} />
      ) : (
        <TaskForm draft={state.draft} dispatch={dispatch} />
      )}
      <nav className="links">
        <button type="button" className="link" onClick={() => setHistoryOpen(true)}>
          History
        </button>
        {user.role === "admin" && (
          <>
            {" · "}
            <Link to="/admin">Admin</Link>
          </>
        )}
        {" · "}
        <button type="button" className="link" onClick={onLogOut}>
          Log out
        </button>
      </nav>
      {historyOpen && <History onClose={closeHistory} />}
    </main>
  );
}
