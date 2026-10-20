import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, NavLink, Route, Routes } from "react-router-dom";
import "./styles.css";
import { Annotate } from "./pages/Annotate";
import { Calibration } from "./pages/Calibration";
import { Conversation } from "./pages/Conversation";
import { Conversations } from "./pages/Conversations";
import { Experiments } from "./pages/Experiments";
import { Scores } from "./pages/Scores";
import { Settings } from "./pages/Settings";
import { useApi } from "./hooks";

function Layout() {
  const me = useApi("/v1/me");
  return (
    <div className="app">
      <nav className="side">
        <div className="brand">elastic-evals</div>
        <NavLink to="/" end>Conversations</NavLink>
        <NavLink to="/scores">Scores &amp; calibration</NavLink>
        <NavLink to="/experiments">Experiments</NavLink>
        <NavLink to="/annotate">Annotation</NavLink>
        <NavLink to="/settings">Settings</NavLink>
        <div className="tenant">{me.data ? `tenant ${me.data.tenant}` : me.error ? "not signed in" : ""}</div>
      </nav>
      <main>
        <Routes>
          <Route path="/" element={<Conversations />} />
          <Route path="/conversations" element={<Conversations />} />
          <Route path="/conversations/:id" element={<Conversation />} />
          <Route path="/scores" element={<Scores />} />
          <Route path="/calibrations/:id" element={<Calibration />} />
          <Route path="/experiments" element={<Experiments />} />
          <Route path="/annotate" element={<Annotate />} />
          <Route path="/settings" element={<Settings />} />
        </Routes>
      </main>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <Layout />
    </BrowserRouter>
  </StrictMode>,
);
