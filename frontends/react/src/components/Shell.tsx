// The page frame: sign-in guard, header (navigation of the role, light/dark, reload, account), the scope line and the
// page body. On a phone the navigation collapses into a menu. Same rules as the NiceGUI and Streamlit frames.
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { api, ApiError, session, type Answer } from "../api/client";
import { isDark, setThemeChoice, tone } from "../theme";
import { Badge, ErrorBox, Icon, Mark } from "./ui";

export type Workspace = Answer<"/api/v1/workspace">;
const WorkspaceContext = createContext<{ info: Workspace; refresh: () => void } | null>(null);

export function useWorkspace() {
  const value = useContext(WorkspaceContext);
  if (!value) throw new Error("useWorkspace outside the page frame");
  return value;
}

// the heading of a page: the words of its navigation entry for this role ("My projects" for a Project Manager)
export function usePageTitle(pageId: string, fallback: string) {
  const { info } = useWorkspace();
  return info.pages.find((p) => p.id === pageId)?.title ?? fallback;
}

// the link between a page and its sub-page (Projects ⇄ Data quality, Steering Console ⇄ What-if comparison)
export const PageLink = ({ to, icon, text }: { to: string; icon: string; text: string }) => (
  <Link className="row link-button" to={`/${to}`}><Icon name={icon} />{text}</Link>
);

export const scopeText = (scope: NonNullable<Workspace["scope"]>) =>
  scope.total == null ? `${scope.visible} project${scope.visible === 1 ? "" : "s"}` : `${scope.visible} of ${scope.total} projects`;

const title = (page: Workspace["pages"][number], info: Workspace) =>
  page.id === "approvals" && info.pending_approvals ? `${page.title} (${info.pending_approvals})` : page.title;

export function Shell({ pageId, children }: { pageId: string; children: ReactNode }) {
  const [info, setInfo] = useState<Workspace | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [menu, setMenu] = useState<"nav" | "account" | null>(null);
  const [dark, setDark] = useState(isDark());
  const navigate = useNavigate();
  const location = useLocation();
  const toSignIn = useCallback(
    () => navigate(`/sign-in?next=${encodeURIComponent(location.pathname + location.search)}`, { replace: true }),
    [navigate, location],
  );

  const load = useCallback(async () => {
    if (!session.token()) return toSignIn();
    try {
      setInfo(await api.get<Workspace>("/workspace"));
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return toSignIn();
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [toSignIn]);

  useEffect(() => {
    void load();
  }, [load]);
  useEffect(() => setMenu(null), [location]);

  if (error) return <div className="page"><ErrorBox message={error} /></div>;
  if (!info) return <div className="page muted" aria-busy="true">Loading …</div>;
  const ids = info.pages.map((p) => p.id);
  if (!ids.includes(pageId)) {
    navigate(`/${ids[0]}`, { replace: true }); // a link to a page this role does not have
    return null;
  }
  // sub-pages (Data quality, What-if comparison) are opened from their parent page, which stays highlighted
  const nav = info.pages.filter((p) => !p.parent);
  const active = info.pages.find((p) => p.id === pageId)?.parent ?? pageId;
  const signOut = async () => {
    await session.signOut();
    navigate("/sign-in");
  };
  const reload = async () => {
    await api.post("/data/reload");
    window.location.reload();
  };
  const flipTheme = () => {
    setThemeChoice(dark ? "light" : "dark");
    setDark(!dark);
  };

  return (
    <WorkspaceContext.Provider value={{ info, refresh: () => void load() }}>
      <header className="header">
        <button className="flat only-mobile" aria-label="Open navigation" onClick={() => setMenu(menu === "nav" ? null : "nav")}>
          <Icon name="menu" />
        </button>
        <Link to={`/${ids[0]}`} aria-label="DerKurator AI home" style={{ textDecoration: "none" }}><Mark /></Link>
        <nav className="nav" aria-label="Pages">
          {nav.map((p) => (
            <Link key={p.id} to={`/${p.id}`} className={p.id === active ? "active" : ""} aria-current={p.id === active ? "page" : undefined}>
              <Icon name={p.icon} />
              {title(p, info)}
            </Link>
          ))}
        </nav>
        <span className="spacer" />
        <button className="flat" aria-label="Switch light/dark theme" title="Light / dark" onClick={flipTheme}>
          <Icon name={dark ? "light_mode" : "dark_mode"} />
        </button>
        {info.scope && (
          <button className="flat" aria-label="Reload portfolio data" title="Reload portfolio data" onClick={() => void reload()}>
            <Icon name="refresh" />
          </button>
        )}
        <button className="flat" aria-label="Account" title={`${info.user.display_name} · ${info.user.role_label}`}
          onClick={() => setMenu(menu === "account" ? null : "account")}>
          <Icon name="account_circle" />
          <span className="hide-narrow">{info.user.display_name} · {info.user.role_label}</span>
        </button>
        {menu === "nav" && (
          <div className="menu left" role="menu">
            {nav.map((p) => <Link key={p.id} to={`/${p.id}`} role="menuitem">{title(p, info)}</Link>)}
          </div>
        )}
        {menu === "account" && (
          <div className="menu" role="menu">
            <strong>{info.user.display_name}</strong>
            <div className="muted">User {info.user.user_id} · {info.user.role_label}</div>
            <div className="muted small">To work as another identity, sign out and sign in again.</div>
            <button style={{ marginTop: 8 }} onClick={signOut}><Icon name="logout" />Sign out</button>
          </div>
        )}
      </header>
      <div className="subbar">
        <span className="muted hide-phone">Enterprise Portfolio Digital Twin</span>
        <span className="spacer" />
        <Badge text={info.environment} tone={tone("environment", info.environment)} icon="dns" />
        <span className="muted">
          {info.scope ? `${scopeText(info.scope)} visible · data ${info.loaded_at}` : "No access to portfolio data"}
        </span>
      </div>
      {info.production && <div className="callout tone-danger" style={{ textAlign: "center", fontWeight: 700 }}>LIVE PRODUCTION ENVIRONMENT</div>}
      <main className="page">{children}</main>
    </WorkspaceContext.Provider>
  );
}
