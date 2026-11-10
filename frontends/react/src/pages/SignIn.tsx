// The only page without a signed-in identity: centred sign-in card, demo accounts in DEV/TEST on demo data.
import { useEffect, useState, type FormEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api, ApiError, session, type Answer } from "../api/client";
import { tone } from "../theme";
import { Badge, ErrorBox, Icon, Logo } from "../components/ui";

type Info = Answer<"/api/v1/sign-in-info">;
type Token = Answer<"/api/v1/auth/token", "post">;

const safeTarget = (next: string | null) => (next && next.startsWith("/") && !next.startsWith("//") ? next : "/");

export function SignIn() {
  const [info, setInfo] = useState<Info | null>(null);
  const [user, setUser] = useState("");
  const [password, setPassword] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [params] = useSearchParams();
  const navigate = useNavigate();

  useEffect(() => {
    api.get<Info>("/sign-in-info").then(setInfo, (e) => setMessage(String(e.message ?? e)));
  }, []);

  const submit = async (userId: string, secret: string) => {
    try {
      const answer = await api.post<Token>("/auth/token", { user_id: userId.trim(), password: secret });
      session.set(answer.access_token);
      navigate(safeTarget(params.get("next")), { replace: true });
    } catch (e) {
      setMessage(e instanceof ApiError ? e.message : String(e));
    }
  };
  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    void submit(user, password);
  };

  return (
    <div className="center">
      <section className="card" style={{ width: "100%", maxWidth: 420, height: "auto" }}>
        <div className="row" style={{ marginBottom: 12 }}>
          <Logo />
          <span className="spacer" />
          {info && <Badge text={info.environment} tone={tone("environment", info.environment)} icon="dns" />}
        </div>
        <h1>Sign in</h1>
        {info && !info.configured ? (
          <ErrorBox message="Sign-in is not configured – contact your System Admin." />
        ) : (
          <form onSubmit={onSubmit} style={{ display: "flex", flexDirection: "column", gap: 12, marginTop: 12 }}>
            <label className="field">User
              <input autoFocus value={user} onChange={(e) => setUser(e.target.value)} placeholder="e.g. PMO-ADMIN or your name" autoComplete="username" />
            </label>
            <label className="field">Password
              <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
            </label>
            {message && <div className="error" role="alert">{message}</div>}
            <button className="primary" type="submit"><Icon name="login" />Sign in</button>
          </form>
        )}
      </section>
      {info?.demo && (
        <section className="card" style={{ width: "100%", maxWidth: 720, height: "auto" }}>
          <strong>Demo environment – demo accounts</strong>
          <div className="muted small" style={{ marginBottom: 8 }}>
            Password {info.demo.password} · shared demo: everyone with the same account sees the same inbox, chats and
            saved what-ifs. A button signs you in as that account.
          </div>
          <div className="grid">
            {info.demo.accounts.map((a) => (
              <button key={a.user_id} onClick={() => void submit(a.user_id, info.demo!.password)}>
                {a.display_name} · {a.role_label}
              </button>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
