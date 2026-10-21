import { useCallback, useEffect, useState, type ReactNode } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { getMe, logOut, NetworkError, onSignedOut } from "./api/http";
import type { User } from "./api/types";
import { CANT_REACH } from "./errors";
import { AdminPage } from "./views/AdminPage";
import { SignIn } from "./views/SignIn";
import { UserPage } from "./views/UserPage";

type Session =
  { state: "loading" } | { state: "signedOut"; notice?: string } | { state: "signedIn"; user: User };

export function App() {
  const [session, setSession] = useState<Session>({ state: "loading" });

  useEffect(() => {
    let current = true;
    getMe().then(
      (user) => current && setSession({ state: "signedIn", user }),
      (error) =>
        current &&
        setSession({ state: "signedOut", notice: error instanceof NetworkError ? CANT_REACH : "" }),
    );
    const stop = onSignedOut(() => setSession({ state: "signedOut" }));
    return () => {
      current = false;
      stop();
    };
  }, []);

  const signedIn = useCallback((user: User) => setSession({ state: "signedIn", user }), []);
  const signOut = useCallback(async () => {
    try {
      await logOut();
    } catch {
      // The cookies may outlive a failed logout, but this tab still forgets the user.
    }
    setSession({ state: "signedOut" });
  }, []);

  function signedInOnly(page: (user: User) => ReactNode) {
    if (session.state === "loading") return null;
    if (session.state === "signedOut") return <SignIn onSignedIn={signedIn} notice={session.notice} />;
    return page(session.user);
  }

  return (
    <Routes>
      <Route
        path="/"
        element={signedInOnly((user) => (
          <UserPage user={user} onLogOut={signOut} />
        ))}
      />
      <Route
        path="/admin"
        element={signedInOnly(() => (
          <AdminPage />
        ))}
      />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
