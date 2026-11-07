import { useState, type FormEvent } from "react";
import { login, type User } from "../services/api";

export default function Login({ onLogin }: { onLogin: (user: User) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onLogin(await login(email.trim(), password));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Login failed");
    } finally {
      setBusy(false);
      setPassword("");
    }
  };

  return (
    <main className="centered">
      <form className="panel login" onSubmit={submit} aria-label="Sign in">
        <h1>HoneyChain</h1>
        <label>
          Email
          <input type="email" autoComplete="username" required value={email}
                 onChange={(e) => setEmail(e.target.value)} />
        </label>
        <label>
          Password
          <input type="password" autoComplete="current-password" required value={password}
                 onChange={(e) => setPassword(e.target.value)} />
        </label>
        {error && <p className="error" role="alert">{error}</p>}
        <button type="submit" disabled={busy}>{busy ? "Signing in..." : "Sign in"}</button>
      </form>
    </main>
  );
}
