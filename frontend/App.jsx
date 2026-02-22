import React, { useState, useEffect } from "react";

const API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000/api/v1";

const MOCK_CHANGES = [
  {
    id: 1,
    severity: "high",
    is_breaking: true,
    summary: "Added required 'priority' field to SessionCreate",
    created_at: new Date().toISOString(),
  },
];

function useMockData() {
  return !import.meta.env.VITE_API_BASE;
}

function MockBanner() {
  return (
    <div
      style={{
        background: "#fff3cd",
        border: "2px solid #ffc107",
        color: "#856404",
        padding: "12px 20px",
        textAlign: "center",
        fontWeight: "bold",
        fontSize: "14px",
      }}
    >
      MOCK DATA - Connect a live API by setting VITE_API_BASE
    </div>
  );
}

export default function App() {
  const isMock = useMockData();
  const [changes, setChanges] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (isMock) {
      setChanges(MOCK_CHANGES);
      setLoading(false);
      return;
    }

    fetch(`${API_BASE}/contracts/changes`)
      .then((r) => r.json())
      .then(setChanges)
      .catch(() => {
        setChanges(MOCK_CHANGES);
      })
      .finally(() => setLoading(false));
  }, [isMock]);

  return (
    <div style={{ fontFamily: "system-ui, sans-serif", maxWidth: 800, margin: "0 auto", padding: 20 }}>
      {isMock && <MockBanner />}
      <h1>Contract Propagation Dashboard</h1>
      {loading ? (
        <p>Loading...</p>
      ) : (
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr>
              <th style={{ textAlign: "left", borderBottom: "2px solid #ddd", padding: 8 }}>ID</th>
              <th style={{ textAlign: "left", borderBottom: "2px solid #ddd", padding: 8 }}>Severity</th>
              <th style={{ textAlign: "left", borderBottom: "2px solid #ddd", padding: 8 }}>Breaking</th>
              <th style={{ textAlign: "left", borderBottom: "2px solid #ddd", padding: 8 }}>Summary</th>
            </tr>
          </thead>
          <tbody>
            {changes.map((c) => (
              <tr key={c.id}>
                <td style={{ padding: 8, borderBottom: "1px solid #eee" }}>{c.id}</td>
                <td style={{ padding: 8, borderBottom: "1px solid #eee" }}>{c.severity}</td>
                <td style={{ padding: 8, borderBottom: "1px solid #eee" }}>{c.is_breaking ? "Yes" : "No"}</td>
                <td style={{ padding: 8, borderBottom: "1px solid #eee" }}>{c.summary}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
