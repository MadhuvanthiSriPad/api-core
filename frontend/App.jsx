import React, { useState, useEffect } from "react";

const API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000/api/v1";

function getSummary(change) {
  if (!change || typeof change !== "object") return "n/a";
  if (typeof change.summary === "string" && change.summary.trim()) return change.summary;
  if (typeof change.summary_json !== "string" || !change.summary_json.trim()) return "n/a";
  try {
    const parsed = JSON.parse(change.summary_json);
    return parsed?.summary || "n/a";
  } catch {
    return "n/a";
  }
}

export default function App() {
  const [changes, setChanges] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;

    async function loadChanges() {
      setLoading(true);
      setError("");
      try {
        const response = await fetch(`${API_BASE}/contracts/changes`);
        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`);
        }
        const data = await response.json();
        if (!Array.isArray(data)) {
          throw new Error("Unexpected response payload");
        }
        if (!cancelled) {
          setChanges(data);
        }
      } catch (err) {
        if (!cancelled) {
          const message = err instanceof Error ? err.message : "Unknown error";
          setChanges([]);
          setError(`Failed to load live contract changes from ${API_BASE}: ${message}`);
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    loadChanges();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div style={{ fontFamily: "system-ui, sans-serif", maxWidth: 800, margin: "0 auto", padding: 20 }}>
      <h1>Contract Propagation Dashboard</h1>
      <p style={{ color: "#555", marginTop: 0 }}>Data source: {API_BASE}</p>
      {loading ? (
        <p>Loading...</p>
      ) : error ? (
        <p style={{ color: "#b91c1c", fontWeight: 600 }}>{error}</p>
      ) : changes.length === 0 ? (
        <p>No contract changes found yet. Run a real propagation to populate this view.</p>
      ) : (
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr>
              <th style={{ textAlign: "left", borderBottom: "2px solid #ddd", padding: 8 }}>ID</th>
              <th style={{ textAlign: "left", borderBottom: "2px solid #ddd", padding: 8 }}>Created</th>
              <th style={{ textAlign: "left", borderBottom: "2px solid #ddd", padding: 8 }}>Severity</th>
              <th style={{ textAlign: "left", borderBottom: "2px solid #ddd", padding: 8 }}>Breaking</th>
              <th style={{ textAlign: "left", borderBottom: "2px solid #ddd", padding: 8 }}>Status</th>
              <th style={{ textAlign: "left", borderBottom: "2px solid #ddd", padding: 8 }}>Summary</th>
            </tr>
          </thead>
          <tbody>
            {changes.map((c) => (
              <tr key={c.id}>
                <td style={{ padding: 8, borderBottom: "1px solid #eee" }}>{c.id}</td>
                <td style={{ padding: 8, borderBottom: "1px solid #eee" }}>
                  {c.created_at ? new Date(c.created_at).toLocaleString() : "n/a"}
                </td>
                <td style={{ padding: 8, borderBottom: "1px solid #eee" }}>{c.severity}</td>
                <td style={{ padding: 8, borderBottom: "1px solid #eee" }}>{c.is_breaking ? "Yes" : "No"}</td>
                <td style={{ padding: 8, borderBottom: "1px solid #eee" }}>{c.remediation_status || "pending"}</td>
                <td style={{ padding: 8, borderBottom: "1px solid #eee" }}>{getSummary(c)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
