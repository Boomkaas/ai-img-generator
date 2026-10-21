import { useState, type FormEvent } from "react";
import { register, signIn } from "../api/http";
import type { User } from "../api/types";
import { errorText } from "../errors";

export function SignIn({ onSignedIn, notice = "" }: { onSignedIn: (user: User) => void; notice?: string }) {
  const [creating, setCreating] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(notice);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!email.trim() || !password) {
      setError("Enter your email and password.");
      return;
    }
    setError("");
    setBusy(true);
    try {
      onSignedIn(await (creating ? register : signIn)(email.trim(), password));
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  }

  function switchMode() {
    setCreating((value) => !value);
    setError("");
  }

  return (
    <main className="column">
      <h1>Generate</h1>
      <form onSubmit={submit} noValidate>
        <label htmlFor="email">Email</label>
        <input
          id="email"
          type="email"
          autoComplete="email"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
        />
        <label htmlFor="password">Password</label>
        <input
          id="password"
          type="password"
          autoComplete={creating ? "new-password" : "current-password"}
          value={password}
          onChange={(event) => setPassword(event.target.value)}
        />
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <button type="submit" className="primary" disabled={busy}>
          {creating ? "Create account" : "Sign in"}
        </button>
      </form>
      <p className="switch">
        {creating ? "Already have an account? " : "New here? "}
        <button type="button" className="link" onClick={switchMode}>
          {creating ? "Sign in" : "Create an account"}
        </button>
      </p>
    </main>
  );
}
