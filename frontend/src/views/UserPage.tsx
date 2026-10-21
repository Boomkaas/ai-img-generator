import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { cancelTask, getActiveTask, getTask } from "../api/http";
import { openSocket } from "../api/socket";
import type { FinishedStatus, TaskOut, User } from "../api/types";
import { retryDelayMs } from "../config";
import { errorText } from "../errors";
import { initialState, isActive, reducer, type Action } from "../state";
import { StatusLine, TaskForm, TaskView } from "./Generator";
import { History } from "./History";

export function UserPage({ user, onLogOut }: { user: User; onLogOut: () => void }) {
  const [state, render] = useReducer(reducer, initialState);
  const [historyOpen, setHistoryOpen] = useState(false);
  // Updated with every dispatch rather than after the next render, so socket messages handled back to back
  // each see the state the one before left.
  const stateRef = useRef(initialState);
  const dispatch = useCallback((action: Action) => {
    stateRef.current = reducer(stateRef.current, action);
    render(action);
  }, []);
  const mounted = useRef(false);

  // Fetch a task that has ended, so the done or failed view has everything it shows. Retried while the task
  // is still on screen as active, so one failed request can't leave the page stuck on the running view.
  const settle = useCallback(
    async (id: string, status?: FinishedStatus) => {
      for (let attempt = 0; mounted.current; attempt += 1) {
        try {
          dispatch({ type: "settled", task: await getTask(id) });
          return;
        } catch {
          const current = stateRef.current.task;
          if (current?.task_id !== id || !isActive(current)) return;
          if (status) {
            dispatch({ type: "settled", task: { ...current, status } });
            return;
          }
          await new Promise((resolve) => setTimeout(resolve, retryDelayMs(attempt)));
        }
      }
    },
    [dispatch],
  );

  useEffect(() => {
    mounted.current = true;
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
      mounted.current = false;
      stopped = true;
      close?.();
    };
  }, [dispatch, settle]);

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
    [dispatch, settle],
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
