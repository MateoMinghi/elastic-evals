import { useState } from "react";
import { saveSettings } from "../api";
import { useApi } from "../hooks";

export function Settings() {
  const [key, setKey] = useState(() => {
    try { return localStorage.getItem("ee.key") || ""; } catch { return ""; }
  });
  const [tenant, setTenant] = useState(() => {
    try { return localStorage.getItem("ee.tenant") || ""; } catch { return ""; }
  });
  const me = useApi("/v1/me");
  return (
    <>
      <h1>Settings</h1>
      <div className="panel" style={{ maxWidth: 560 }}>
        <p className="small muted">The API key stays in this browser. Against a development server (EE_AUTH=off), leave it empty and set a tenant.</p>
        <div className="row" style={{ marginBottom: 8 }}>
          <label style={{ width: 80 }}>API key</label>
          <input style={{ flex: 1 }} value={key} onChange={(e) => setKey(e.target.value)} placeholder="ek_….secret" aria-label="api key" />
        </div>
        <div className="row" style={{ marginBottom: 8 }}>
          <label style={{ width: 80 }}>Tenant</label>
          <input style={{ flex: 1 }} value={tenant} onChange={(e) => setTenant(e.target.value)} placeholder="dev servers only" aria-label="tenant" />
        </div>
        <button className="primary" onClick={() => { saveSettings(key, tenant); me.reload(); }}>Save</button>
        <div className="small" style={{ marginTop: 10 }}>
          {me.data ? `Signed in as ${me.data.actor} (tenant ${me.data.tenant})` : me.error}
        </div>
      </div>
    </>
  );
}
