"use client";

import React, { useEffect, useState } from "react";

export interface LedgerStep {
  seq: number;
  tool: str;
  semantics: "COMPENSABLE" | "REVERSIBLE" | "IRREVERSIBLE";
  status: "INTENT" | "COMMITTED" | "COMPENSATED" | "BLOCKED" | "FAILED";
  timestamp: string;
  duration_ms?: number;
  payload_hash?: str;
}

export interface SagaVisualLedgerProps {
  saga_id: string;
  saga_name?: string;
  steps: LedgerStep[];
  is_clean_finish?: boolean;
  byok_encrypted?: boolean;
  on_trigger_confetti?: () => void;
}

export const SagaVisualLedger: React.FC<SagaVisualLedgerProps> = ({
  saga_id,
  saga_name = "Transactional Tool Workflow",
  steps,
  is_clean_finish = false,
  byok_encrypted = true,
  on_trigger_confetti,
}) => {
  const [activeTab, setActiveTab] = useState<"timeline" | "console">("timeline");
  const [confettiTriggered, setConfettiTriggered] = useState<boolean>(false);

  useEffect(() => {
    if (is_clean_finish && !confettiTriggered) {
      setConfettiTriggered(true);
      if (on_trigger_confetti) {
        on_trigger_confetti();
      }
    }
  }, [is_clean_finish, confettiTriggered, on_trigger_confetti]);

  return (
    <div style={styles.card}>
      {/* HEADER BAR */}
      <div style={styles.header}>
        <div>
          <div style={styles.eyebrow}>SAGAOPS SUPREME VISUAL LEDGER</div>
          <h3 style={styles.title}>{saga_name}</h3>
          <div style={styles.sagaId}>
            WAL Stream ID: <code>{saga_id}</code>
          </div>
        </div>

        <div style={styles.tabGroup}>
          <button
            style={{
              ...styles.tabBtn,
              ...(activeTab === "timeline" ? styles.activeTab : {}),
            }}
            onClick={() => setActiveTab("timeline")}
          >
            📊 Kinetic Flow Timeline
          </button>
          <button
            style={{
              ...styles.tabBtn,
              ...(activeTab === "console" ? styles.activeTab : {}),
            }}
            onClick={() => setActiveTab("console")}
          >
            💻 WAL Console Inspector
          </button>
        </div>
      </div>

      {/* CELEBRATION BADGE ON CLEAN FINISH */}
      {is_clean_finish && (
        <div style={styles.celebrationBanner}>
          <span style={{ fontSize: "1.3rem" }}>🎉</span>
          <div>
            <b>100% Clean Transaction Commitment</b>
            <p style={{ margin: "0.15rem 0 0", fontSize: "0.83rem", opacity: 0.9 }}>
              All WAL records committed with zero unhandled side effects or orphaned resources.
            </p>
          </div>
        </div>
      )}

      {/* TAB CONTENT: TIMELINE */}
      {activeTab === "timeline" && (
        <div style={styles.timelineList}>
          {steps.map((st) => (
            <div key={st.seq} style={styles.stepNode}>
              <div
                style={{
                  ...styles.seqBadge,
                  ...(st.status === "COMMITTED"
                    ? styles.seqCommitted
                    : st.status === "COMPENSATED"
                    ? styles.seqCompensated
                    : styles.seqBlocked),
                }}
              >
                #{st.seq}
              </div>

              <div style={{ flex: 1 }}>
                <div style={styles.nodeHeader}>
                  <b style={{ fontSize: "0.95rem" }}>{st.tool}</b>
                  <span style={styles.semanticsPill}>{st.semantics}</span>
                </div>
                <div style={styles.timestampRow}>
                  <span>Logged: {st.timestamp}</span>
                  {st.payload_hash && (
                    <span style={styles.hashSpan}>SHA-256: {st.payload_hash.substring(0, 12)}...</span>
                  )}
                </div>
              </div>

              <div style={{ textAlign: "right" }}>
                <span
                  style={{
                    ...styles.statusBadge,
                    ...(st.status === "COMMITTED"
                      ? styles.statusCommitted
                      : st.status === "COMPENSATED"
                      ? styles.statusCompensated
                      : styles.statusBlocked),
                  }}
                >
                  {st.status}
                </span>
                {st.duration_ms && <div style={styles.duration}>{st.duration_ms}ms</div>}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* TAB CONTENT: CONSOLE */}
      {activeTab === "console" && (
        <div style={styles.consoleBox}>
          <div style={styles.consoleHeader}>
            <span>REAL-TIME WAL TERMINAL INSPECTOR</span>
            <span style={{ color: byok_encrypted ? "#10b981" : "#f59e0b" }}>
              {byok_encrypted ? "🔐 BYOK FERNET ENCRYPTED" : "PLAINTEXT LOG"}
            </span>
          </div>
          <pre style={styles.consolePre}>
            {steps
              .map(
                (st) =>
                  `[WAL #${String(st.seq).padStart(4, "0")}] ts=${st.timestamp} tool=${st.tool} semantics=${st.semantics} status=${st.status} hash=${st.payload_hash || "e3b0c44298fc"}`
              )
              .join("\n")}
          </pre>
        </div>
      )}

      <footer style={styles.footer}>
        <span>Published & Maintained by <b>SAGAOPS Enterprise</b> · Founded & Owned by <b>Thomas J George</b></span>
        <span>Contact: thomasjgeorge23@gmail.com</span>
      </footer>
    </div>
  );
};

const styles: { [key: string]: React.CSSProperties } = {
  card: {
    background: "rgba(15, 23, 42, 0.95)",
    backdropFilter: "blur(12px)",
    border: "1px solid rgba(255, 255, 255, 0.1)",
    borderRadius: "18px",
    padding: "1.8rem",
    color: "#f8fafc",
    fontFamily: 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif',
    boxShadow: "0 20px 50px rgba(0, 0, 0, 0.5)",
  },
  header: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    marginBottom: "1.5rem",
    borderBottom: "1px solid rgba(255,255,255,0.08)",
    paddingBottom: "1rem",
    flexWrap: "wrap",
    gap: "1rem",
  },
  eyebrow: {
    fontFamily: "monospace",
    fontSize: "0.75rem",
    color: "#a855f7",
    fontWeight: 700,
    letterSpacing: "0.15em",
  },
  title: {
    fontSize: "1.5rem",
    fontWeight: 900,
    margin: "0.2rem 0",
  },
  sagaId: {
    fontSize: "0.85rem",
    color: "#94a3b8",
  },
  tabGroup: {
    display: "flex",
    gap: "0.5rem",
    background: "#0f172a",
    padding: "0.3rem",
    borderRadius: "10px",
    border: "1px solid #1e293b",
  },
  tabBtn: {
    background: "transparent",
    color: "#94a3b8",
    border: "none",
    padding: "0.5rem 0.9rem",
    borderRadius: "8px",
    fontSize: "0.85rem",
    fontWeight: 600,
    cursor: "pointer",
  },
  activeTab: {
    background: "#1e293b",
    color: "#f8fafc",
  },
  celebrationBanner: {
    background: "linear-gradient(135deg, rgba(16, 185, 129, 0.2), rgba(5, 150, 105, 0.1))",
    border: "1px solid rgba(16, 185, 129, 0.4)",
    borderRadius: "12px",
    padding: "1rem 1.2rem",
    display: "flex",
    alignItems: "center",
    gap: "0.8rem",
    color: "#34d399",
    marginBottom: "1.5rem",
  },
  timelineList: {
    display: "flex",
    flexDirection: "column",
    gap: "0.85rem",
  },
  stepNode: {
    display: "flex",
    alignItems: "center",
    background: "#0b1329",
    border: "1px solid rgba(255,255,255,0.06)",
    borderRadius: "12px",
    padding: "0.9rem 1.1rem",
    gap: "1rem",
  },
  seqBadge: {
    fontFamily: "monospace",
    fontWeight: 800,
    fontSize: "0.82rem",
    padding: "0.3rem 0.6rem",
    borderRadius: "8px",
  },
  seqCommitted: {
    background: "rgba(16, 185, 129, 0.15)",
    color: "#10b981",
  },
  seqCompensated: {
    background: "rgba(245, 158, 11, 0.15)",
    color: "#f59e0b",
  },
  seqBlocked: {
    background: "rgba(239, 68, 68, 0.15)",
    color: "#ef4444",
  },
  nodeHeader: {
    display: "flex",
    alignItems: "center",
    gap: "0.6rem",
  },
  semanticsPill: {
    fontFamily: "monospace",
    fontSize: "0.72rem",
    background: "#1e293b",
    color: "#a855f7",
    padding: "0.15rem 0.45rem",
    borderRadius: "4px",
  },
  timestampRow: {
    fontSize: "0.78rem",
    color: "#64748b",
    marginTop: "0.25rem",
    display: "flex",
    gap: "1rem",
  },
  hashSpan: {
    fontFamily: "monospace",
    color: "#94a3b8",
  },
  statusBadge: {
    fontSize: "0.75rem",
    fontWeight: 800,
    padding: "0.25rem 0.6rem",
    borderRadius: "6px",
  },
  statusCommitted: {
    background: "rgba(16, 185, 129, 0.2)",
    color: "#10b981",
  },
  statusCompensated: {
    background: "rgba(245, 158, 11, 0.2)",
    color: "#f59e0b",
  },
  statusBlocked: {
    background: "rgba(239, 68, 68, 0.2)",
    color: "#ef4444",
  },
  duration: {
    fontSize: "0.75rem",
    color: "#64748b",
    marginTop: "0.2rem",
  },
  consoleBox: {
    background: "#050810",
    border: "1px solid #1e293b",
    borderRadius: "12px",
    padding: "1rem",
  },
  consoleHeader: {
    display: "flex",
    justifyContent: "space-between",
    fontFamily: "monospace",
    fontSize: "0.78rem",
    color: "#64748b",
    marginBottom: "0.75rem",
    borderBottom: "1px solid #1e293b",
    paddingBottom: "0.5rem",
  },
  consolePre: {
    fontFamily: "monospace",
    fontSize: "0.82rem",
    color: "#38bdf8",
    margin: 0,
    whiteSpace: "pre-wrap",
    lineHeight: 1.6,
  },
  footer: {
    marginTop: "1.5rem",
    paddingTop: "1rem",
    borderTop: "1px solid rgba(255,255,255,0.06)",
    display: "flex",
    justifyContent: "space-between",
    fontSize: "0.8rem",
    color: "#64748b",
  },
};

export default SagaVisualLedger;
