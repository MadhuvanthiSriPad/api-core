import React, { useEffect, useMemo, useState } from "react";

const API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000/api/v1";

function safeParseJsonArray(raw) {
  if (typeof raw !== "string" || !raw.trim()) return [];
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

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

function getChangedRoutes(change) {
  if (!change || typeof change !== "object") return [];
  if (Array.isArray(change.changed_routes)) return change.changed_routes;
  return safeParseJsonArray(change.changed_routes_json).filter((route) => typeof route === "string");
}

function buildOpenApiTriggerDiffHunks() {
  return [
    {
      id: "Breaking #1",
      title: "A new required max_cost_usd field on session creation",
      lines: [
        "@@ POST /sessions request body @@",
        " required:",
        "   - team_id",
        "   - agent_name",
        "   - priority",
        "+  - max_cost_usd",
      ],
    },
    {
      id: "Breaking #2",
      title: "usage.cached_tokens renamed to usage.cache_read_tokens",
      lines: [
        "@@ GET /sessions response @@",
        " usage:",
        "   input_tokens: integer",
        "   output_tokens: integer",
        "-  cached_tokens: integer",
        "+  cache_read_tokens: integer",
      ],
    },
    {
      id: "Breaking #3",
      title: "billing.total renamed to billing.total_usd",
      lines: [
        "@@ GET /sessions response @@",
        " billing:",
        "-  total:",
        "+  total_usd:",
        "     type: number",
      ],
    },
  ];
}

function getBlastRadius(detail, routes) {
  if (!detail || typeof detail !== "object") {
    return { serviceCount: 0, routeCount: 0, callsLast7d: 0, impactedServices: [] };
  }

  const impactSets = Array.isArray(detail.impact_sets) ? detail.impact_sets : [];
  const impactedServices =
    Array.isArray(detail.impacted_services) && detail.impacted_services.length > 0
      ? detail.impacted_services
      : [...new Set(impactSets.map((item) => item?.caller_service).filter(Boolean))].sort();

  const serviceCount =
    typeof detail.affected_services === "number" ? detail.affected_services : impactedServices.length;

  const routeCount =
    typeof detail.affected_routes === "number"
      ? detail.affected_routes
      : routes.length > 0
        ? routes.length
        : new Set(impactSets.map((item) => `${item?.method || ""} ${item?.route_template || ""}`.trim())).size;

  const callsLast7d =
    typeof detail.total_calls_last_7d === "number"
      ? detail.total_calls_last_7d
      : impactSets.reduce((sum, item) => sum + (Number(item?.calls_last_7d) || 0), 0);

  return { serviceCount, routeCount, callsLast7d, impactedServices };
}

export default function App() {
  const [changes, setChanges] = useState([]);
  const [loadingChanges, setLoadingChanges] = useState(true);
  const [changesError, setChangesError] = useState("");
  const [selectedChangeId, setSelectedChangeId] = useState(null);
  const [changeDetail, setChangeDetail] = useState(null);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [detailError, setDetailError] = useState("");

  useEffect(() => {
    let cancelled = false;

    async function loadChanges() {
      setLoadingChanges(true);
      setChangesError("");
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
          setSelectedChangeId((current) => {
            if (data.length === 0) return null;
            if (current && data.some((item) => item.id === current)) return current;
            return data[0].id;
          });
        }
      } catch (err) {
        if (!cancelled) {
          const message = err instanceof Error ? err.message : "Unknown error";
          setChanges([]);
          setSelectedChangeId(null);
          setChangesError(`Failed to load live contract changes from ${API_BASE}: ${message}`);
        }
      } finally {
        if (!cancelled) {
          setLoadingChanges(false);
        }
      }
    }

    loadChanges();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (selectedChangeId == null) {
      setChangeDetail(null);
      setDetailError("");
      return;
    }

    const controller = new AbortController();

    async function loadDetail() {
      setLoadingDetail(true);
      setDetailError("");
      try {
        const response = await fetch(`${API_BASE}/contracts/changes/${selectedChangeId}`, {
          signal: controller.signal,
        });
        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`);
        }
        const data = await response.json();
        setChangeDetail(data);
      } catch (err) {
        if (controller.signal.aborted) return;
        const message = err instanceof Error ? err.message : "Unknown error";
        setChangeDetail(null);
        setDetailError(`Failed to load change detail from ${API_BASE}: ${message}`);
      } finally {
        if (!controller.signal.aborted) {
          setLoadingDetail(false);
        }
      }
    }

    loadDetail();
    return () => controller.abort();
  }, [selectedChangeId]);

  const selectedChange = useMemo(
    () => changes.find((item) => item.id === selectedChangeId) || null,
    [changes, selectedChangeId]
  );

  const changedRoutes = useMemo(() => {
    if (changeDetail) return getChangedRoutes(changeDetail);
    if (selectedChange) return getChangedRoutes(selectedChange);
    return [];
  }, [changeDetail, selectedChange]);

  const openApiTriggerDiffHunks = useMemo(() => buildOpenApiTriggerDiffHunks(), []);

  const blastRadius = useMemo(
    () => getBlastRadius(changeDetail, changedRoutes),
    [changeDetail, changedRoutes]
  );

  const impactRows = useMemo(() => {
    if (!changeDetail || !Array.isArray(changeDetail.impact_sets)) return [];
    return [...changeDetail.impact_sets].sort(
      (a, b) => (Number(b.calls_last_7d) || 0) - (Number(a.calls_last_7d) || 0)
    );
  }, [changeDetail]);

  const showHighSeverityNarrative =
    changeDetail &&
    changeDetail.is_breaking === true &&
    String(changeDetail.severity || "").toLowerCase() === "high";

  return (
    <div style={{ fontFamily: "system-ui, sans-serif", maxWidth: 1100, margin: "0 auto", padding: 20 }}>
      <h1>Contract Propagation Dashboard</h1>
      <p style={{ color: "#555", marginTop: 0 }}>Data source: {API_BASE}</p>
      {loadingChanges ? (
        <p>Loading...</p>
      ) : changesError ? (
        <p style={{ color: "#b91c1c", fontWeight: 600 }}>{changesError}</p>
      ) : changes.length === 0 ? (
        <p>No contract changes found yet. Run a real propagation to populate this view.</p>
      ) : (
        <>
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
              {changes.map((c) => {
                const isSelected = c.id === selectedChangeId;
                return (
                  <tr
                    key={c.id}
                    onClick={() => setSelectedChangeId(c.id)}
                    style={{ background: isSelected ? "#f8fafc" : "transparent", cursor: "pointer" }}
                  >
                    <td style={{ padding: 8, borderBottom: "1px solid #eee", fontWeight: isSelected ? 700 : 400 }}>
                      {c.id}
                    </td>
                    <td style={{ padding: 8, borderBottom: "1px solid #eee" }}>
                      {c.created_at ? new Date(c.created_at).toLocaleString() : "n/a"}
                    </td>
                    <td style={{ padding: 8, borderBottom: "1px solid #eee" }}>{c.severity}</td>
                    <td style={{ padding: 8, borderBottom: "1px solid #eee" }}>{c.is_breaking ? "Yes" : "No"}</td>
                    <td style={{ padding: 8, borderBottom: "1px solid #eee" }}>
                      {c.remediation_status || "pending"}
                    </td>
                    <td style={{ padding: 8, borderBottom: "1px solid #eee" }}>{getSummary(c)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>

          <section style={{ marginTop: 24, padding: 16, border: "1px solid #e5e7eb", borderRadius: 10 }}>
            <h2 style={{ marginTop: 0, marginBottom: 8 }}>
              Contract Change Detail {selectedChangeId ? `#${selectedChangeId}` : ""}
            </h2>
            {loadingDetail ? (
              <p>Loading change detail...</p>
            ) : detailError ? (
              <p style={{ color: "#b91c1c", fontWeight: 600 }}>{detailError}</p>
            ) : !changeDetail ? (
              <p>Select a contract change to inspect blast radius.</p>
            ) : (
              <>
                <section
                  style={{
                    border: "1px solid #fecaca",
                    borderRadius: 10,
                    padding: 12,
                    marginBottom: 14,
                    background: "#fff1f2",
                  }}
                >
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center",
                      gap: 10,
                      flexWrap: "wrap",
                    }}
                  >
                    <div style={{ fontWeight: 700 }}>
                      openapi.yaml
                      <span style={{ fontWeight: 500, color: "#6b7280", marginLeft: 8 }}>Unified diff</span>
                    </div>
                    <span
                      style={{
                        background: "#b91c1c",
                        color: "#fff",
                        borderRadius: 999,
                        padding: "4px 10px",
                        fontSize: 12,
                        fontWeight: 700,
                        whiteSpace: "nowrap",
                      }}
                    >
                      Breaking change • High severity • 3 changes
                    </span>
                  </div>

                  <div style={{ display: "grid", gap: 10, marginTop: 10 }}>
                    {openApiTriggerDiffHunks.map((hunk) => (
                      <div
                        key={hunk.id}
                        style={{
                          border: "1px solid #fca5a5",
                          borderRadius: 8,
                          overflow: "hidden",
                          background: "#fff",
                        }}
                      >
                        <div
                          style={{
                            display: "flex",
                            justifyContent: "space-between",
                            alignItems: "center",
                            padding: "6px 10px",
                            background: "#fee2e2",
                            borderBottom: "1px solid #fecaca",
                          }}
                        >
                          <strong style={{ fontSize: 13 }}>{hunk.id}</strong>
                          <span style={{ fontSize: 12, color: "#7f1d1d", fontWeight: 600 }}>{hunk.title}</span>
                        </div>
                        <div
                          style={{
                            fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
                            fontSize: 12,
                            lineHeight: 1.5,
                          }}
                        >
                          {hunk.lines.map((line, index) => {
                            const prefix = line.charAt(0);
                            const isAdded = prefix === "+";
                            const isRemoved = prefix === "-";
                            const rowBackground = isAdded ? "#ecfdf3" : isRemoved ? "#fff1f2" : "#f8fafc";
                            const rowColor = isAdded ? "#065f46" : isRemoved ? "#9f1239" : "#374151";
                            return (
                              <div
                                key={`${hunk.id}-${index}`}
                                style={{
                                  background: rowBackground,
                                  color: rowColor,
                                  padding: "2px 10px",
                                  borderBottom: index === hunk.lines.length - 1 ? "none" : "1px solid #f1f5f9",
                                }}
                              >
                                {line}
                              </div>
                            );
                          })}
                        </div>
                      </div>
                    ))}
                  </div>
                </section>

                <p style={{ marginTop: 0, color: "#111827", fontWeight: 600 }}>
                  {showHighSeverityNarrative
                    ? "Within seconds, the engine has diffed the old and new contracts, classified this as a high-severity breaking change, and mapped the blast radius."
                    : `Within seconds, the engine has diffed the old and new contracts, classified this as a ${changeDetail.severity}-severity ${changeDetail.is_breaking ? "breaking" : "non-breaking"} change, and mapped the blast radius.`}
                </p>

                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))",
                    gap: 10,
                    marginBottom: 14,
                  }}
                >
                  <div style={{ padding: 10, border: "1px solid #e5e7eb", borderRadius: 8 }}>
                    <div style={{ fontSize: 12, color: "#6b7280" }}>Affected Services</div>
                    <div style={{ fontSize: 24, fontWeight: 700 }}>{blastRadius.serviceCount}</div>
                  </div>
                  <div style={{ padding: 10, border: "1px solid #e5e7eb", borderRadius: 8 }}>
                    <div style={{ fontSize: 12, color: "#6b7280" }}>Changed Routes</div>
                    <div style={{ fontSize: 24, fontWeight: 700 }}>{blastRadius.routeCount}</div>
                  </div>
                  <div style={{ padding: 10, border: "1px solid #e5e7eb", borderRadius: 8 }}>
                    <div style={{ fontSize: 12, color: "#6b7280" }}>7-Day Call Volume</div>
                    <div style={{ fontSize: 24, fontWeight: 700 }}>{blastRadius.callsLast7d.toLocaleString()}</div>
                  </div>
                  <div style={{ padding: 10, border: "1px solid #e5e7eb", borderRadius: 8 }}>
                    <div style={{ fontSize: 12, color: "#6b7280" }}>Severity</div>
                    <div style={{ fontSize: 24, fontWeight: 700 }}>
                      {String(changeDetail.severity || "n/a").toUpperCase()}
                    </div>
                  </div>
                </div>

                <p style={{ marginTop: 0, marginBottom: 8, color: "#374151" }}>
                  Blast radius: {blastRadius.serviceCount} services, {blastRadius.routeCount} routes,{" "}
                  {blastRadius.callsLast7d.toLocaleString()} calls over the last 7 days.
                </p>

                <div style={{ marginBottom: 14 }}>
                  <div style={{ fontSize: 13, color: "#6b7280", marginBottom: 6 }}>Impacted Services</div>
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                    {blastRadius.impactedServices.map((service) => (
                      <span
                        key={service}
                        style={{
                          background: "#eef2ff",
                          color: "#3730a3",
                          border: "1px solid #c7d2fe",
                          borderRadius: 999,
                          padding: "4px 10px",
                          fontWeight: 600,
                          fontSize: 13,
                        }}
                      >
                        {service}
                      </span>
                    ))}
                  </div>
                </div>

                <div style={{ marginBottom: 14 }}>
                  <div style={{ fontSize: 13, color: "#6b7280", marginBottom: 6 }}>Changed Routes</div>
                  {changedRoutes.length === 0 ? (
                    <p style={{ margin: 0, color: "#6b7280" }}>No changed routes recorded.</p>
                  ) : (
                    <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                      {changedRoutes.map((route) => (
                        <span
                          key={route}
                          style={{
                            background: "#f8fafc",
                            border: "1px solid #e5e7eb",
                            borderRadius: 6,
                            padding: "4px 8px",
                            fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
                            fontSize: 12,
                          }}
                        >
                          {route}
                        </span>
                      ))}
                    </div>
                  )}
                </div>

                <div>
                  <div style={{ fontSize: 13, color: "#6b7280", marginBottom: 6 }}>7-Day Call Volumes</div>
                  {impactRows.length === 0 ? (
                    <p style={{ margin: 0, color: "#6b7280" }}>No impact telemetry found.</p>
                  ) : (
                    <table style={{ width: "100%", borderCollapse: "collapse" }}>
                      <thead>
                        <tr>
                          <th style={{ textAlign: "left", borderBottom: "2px solid #ddd", padding: 8 }}>Service</th>
                          <th style={{ textAlign: "left", borderBottom: "2px solid #ddd", padding: 8 }}>Method</th>
                          <th style={{ textAlign: "left", borderBottom: "2px solid #ddd", padding: 8 }}>Route</th>
                          <th style={{ textAlign: "right", borderBottom: "2px solid #ddd", padding: 8 }}>
                            Calls (7d)
                          </th>
                          <th style={{ textAlign: "left", borderBottom: "2px solid #ddd", padding: 8 }}>
                            Confidence
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        {impactRows.map((row) => (
                          <tr key={`${row.id}-${row.caller_service}-${row.route_template}-${row.method || "ANY"}`}>
                            <td style={{ padding: 8, borderBottom: "1px solid #eee" }}>{row.caller_service}</td>
                            <td style={{ padding: 8, borderBottom: "1px solid #eee" }}>{row.method || "ANY"}</td>
                            <td style={{ padding: 8, borderBottom: "1px solid #eee" }}>
                              <span style={{ fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", fontSize: 12 }}>
                                {row.route_template}
                              </span>
                            </td>
                            <td style={{ padding: 8, borderBottom: "1px solid #eee", textAlign: "right" }}>
                              {Number(row.calls_last_7d || 0).toLocaleString()}
                            </td>
                            <td style={{ padding: 8, borderBottom: "1px solid #eee" }}>{row.confidence}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </div>
              </>
            )}
          </section>
        </>
      )}
    </div>
  );
}
