import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import "material-symbols/outlined.css";
import "./styles.css";
import { installTheme } from "./theme";
import { Shell } from "./components/Shell";
import { SignIn } from "./pages/SignIn";
import { PAGE_IDS, pageComponent } from "./pages";

installTheme();

function App() {
  return (
    <Routes>
      <Route path="/sign-in" element={<SignIn />} />
      {PAGE_IDS.map((id) => {
        const Page = pageComponent(id);
        return <Route key={id} path={`/${id}`} element={<Shell pageId={id}><Page /></Shell>} />;
      })}
      <Route path="*" element={<Navigate to="/overview" replace />} />
    </Routes>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
);
