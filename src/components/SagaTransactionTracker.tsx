"use client";

import React from "react";

export interface TransactionStep {
  id: string;
  step_number: number;
  tool_name: string;
  semantics: "COMPENSABLE" | "REVERSIBLE" | "IRREVERSIBLE";
  status: "COMMITTED" | "COMPENSATED" | "FAILED" | "ORPHANED" | "HALTED";
  timestamp: string;
  inverse_tool?: string;
  latency_ms?: number;
}

export interface SagaTransactionTrackerProps {
  saga_id: string;
  saga_name?: string;
  steps: TransactionStep[];
  overall_status: "SUCCESS" | "CLEAN_ROLLBACK" | "HALTED" | "RUNNING";
  wal_durability: "HASH_CHAINED_FSYNC" | "ENCRYPTED_BYOK" | "MEMORY_ONLY";
  clean_lifo_compensation: boolean;
}

export const SagaTransactionTracker: React.FC<SagaTransactionTrackerProps> = ({
  saga_id,
  saga_name = "Enterprise Transactional Tool Workflow",
  steps,
  overall_status,
  wal_durability,
  clean_lifo_compensation,
}) => {
  return (
    <div style={styles.card}>
      {/* HEADER SECTION */}
      <div style={styles.header}>
        <div>
          <div style={styles.eyebrow}>SAGAOPS REAL-TIME TRANSACTION TRACKER</div>
          <h2 style={styles.sagaTitle}>{saga_name}</h2>
          <div style={styles.sagaId}>
            Saga ID: <code>{saga_id}</code>
          </div>
        </div>

        <div style={styles.badgeGroup}>
          {/* WAL DURABILITY BADGE */}
          <span style={styles.walBadge}>
            ⚡ WAL: {wal_durability === "HASH_CHAINED_FSYNC" ? "HASH-CHAINED FSYNC" : wal_durability}
          </span>

          {/* OVERALL STATUS BADGE */}
          <span
            style={{
              ...styles.statusBadge,
              ...(overall_status === "SUCCESS"
                ? styles.statusSuccess
                : overall_status === "CLEAN_ROLLBACK"
                ? styles.statusRollback
                : styles.statusHalted),
            }}
          >
            {overall_status === "CLEAN_ROLLBACK" ? "✓ 100% CLEAN ROLLBACK" : overall_status}
          </span>
        </div>
      </div>

      {/* 100% CLEAN LIFO COMPENSATION ALERT */}
      {clean_lifo_compensation && (
        <div style={styles.alertClean}>
          <span style={{ fontSize: "1.2rem", marginRight: "0.5rem" }}>🛡️</span>
          <div>
            <b>100% Clean LIFO Compensation Guarantee Enforced</b>
            <p style={{ margin: "0.2rem 0 0", fontSize: "0.85rem", opacity: 0.9 }}>
              All side effects were completely unwound in exact reverse order (LIFO). 0 orphan steps left in production.
            </p>
          </div>
        </div>
      )}

      {/* STEP TIMELINE EXECUTION */}
      <div style={styles.timelineContainer}>
        <h4 style={styles.timelineHeader}>Execution Log & Reversibility Timeline</h4>
        <div style={styles.stepList}>
          {steps.map((st, idx) => (
            <div key={st.id || idx} style={styles.stepRow}>
              <div style={styles.stepIndex}>#{st.step_number}</div>

              <div style={{ flex: 1 }}>
                <div style={styles.stepTitleRow}>
                  <b style={styles.toolName}>{st.tool_name}</b>
                  <span style={styles.semanticsTag}>{st.semantics}</span>
                </div>
                {st.inverse_tool && (
                  <div style={styles.inverseSub}>
                    ↩ Inverse Handler: <code>{st.inverse_tool}</code>
                  </div>
                )}
              </div>

              <div style={{ textAlign: "right" }}>
                <span
                  style={{
                    ...styles.stepStatusBadge,
                    ...(st.status === "COMMITTED"
                      ? styles.stepCommitted
                      : st.status === "COMPENSATED"
                      ? styles.stepCompensated
                      : styles.stepFailed),
                  }}
                >
                  {st.status}
                </span>
                {st.latency_ms && <div style={styles.latency}>{st.latency_ms}ms</div>}
              </div>
            </div>
          ))}
        </div>
      </div>

      <footer style={styles.footer}>
        <span>SAGAOPS Enterprise Kernel · Founded & Owned by <b>Thomas J George</b></span>
        <span>Contact: thomasjgeorge23@gmail.com</span>
      </footer>
    </div>
  );
};

const styles: { [key: string]: React.CSSProperties } = {
  card: {
    background: "#0b1329",
    border: "1px solid #1e293b",
    borderRadius: "16px",
    padding: "1.8rem",
    color: "#f8fafc",
    fontFamily: 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif',
    boxShadow: "0 12px 36px rgba(0,0,0,0.5)",
  },
  header: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "flex-start",
    borderBottom: "1px solid #1e293b",
    paddingBottom: "1.2rem",
    marginBottom: "1.2rem",
    flexWrap: "wrap",
    gap: "1rem",
  },
  eyebrow: {
    fontFamily: "monospace",
    fontSize: "0.75rem",
    fontWeight: 700,
    color: "#38bdf8",
    letterSpacing: "0.1em",
  },
  sagaTitle: {
    fontSize: "1.4rem",
    fontWeight: 800,
    margin: "0.2rem 0 0.4rem",
  },
  sagaId: {
    fontSize: "0.85rem",
    color: "#94a3b8",
  },
  badgeGroup: {
    display: "flex",
    gap: "0.6rem",
    alignItems: "center",
  },
  walBadge: {
    background: "rgba(56, 189, 248, 0.12)",
    color: "#38bdf8",
    border: "1px solid rgba(56, 189, 248, 0.3)",
    fontSize: "0.75rem",
    fontWeight: 700,
    padding: "0.35rem 0.75rem",
    borderRadius: "999px",
  },
  statusBadge: {
    fontSize: "0.75rem",
    fontWeight: 800,
    padding: "0.35rem 0.75rem",
    borderRadius: "999px",
  },
  statusSuccess: {
    background: "rgba(16, 185, 129, 0.2)",
    color: "#10b981",
    border: "1px solid rgba(16, 185, 129, 0.4)",
  },
  statusRollback: {
    background: "rgba(245, 158, 11, 0.2)",
    color: "#f59e0b",
    border: "1px solid rgba(245, 158, 11, 0.4)",
  },
  statusHalted: {
    background: "rgba(239, 68, 68, 0.2)",
    color: "#ef4444",
    border: "1px solid rgba(239, 68, 68, 0.4)",
  },
  alertClean: {
    background: "linear-gradient(135deg, rgba(16, 185, 129, 0.15), rgba(5, 150, 105, 0.1))",
    border: "1px solid rgba(16, 185, 129, 0.35)",
    borderRadius: "12px",
    padding: "1rem 1.2rem",
    display: "flex",
    alignItems: "center",
    marginBottom: "1.5rem",
    color: "#34d399",
  },
  timelineContainer: {
    background: "#070c1b",
    borderRadius: "12px",
    border: "1px solid #162032",
    padding: "1.2rem",
  },
  timelineHeader: {
    margin: "0 0 1rem",
    fontSize: "0.9rem",
    color: "#94a3b8",
    fontWeight: 700,
    textTransform: "uppercase",
    letterSpacing: "0.05em",
  },
  stepList: {
    display: "flex",
    flexDirection: "column",
    gap: "0.75rem",
  },
  stepRow: {
    display: "flex",
    alignItems: "center",
    background: "#0f172a",
    border: "1px solid #1e293b",
    borderRadius: "10px",
    padding: "0.85rem 1rem",
    gap: "1rem",
  },
  stepIndex: {
    fontFamily: "monospace",
    fontWeight: 800,
    color: "#64748b",
    fontSize: "0.85rem",
  },
  stepTitleRow: {
    display: "flex",
    alignItems: "center",
    gap: "0.6rem",
  },
  toolName: {
    fontSize: "0.95rem",
    color: "#f8fafc",
  },
  semanticsTag: {
    fontFamily: "monospace",
    fontSize: "0.7rem",
    background: "#1e293b",
    color: "#38bdf8",
    padding: "0.15rem 0.45rem",
    borderRadius: "4px",
  },
  inverseSub: {
    fontSize: "0.8rem",
    color: "#f59e0b",
    marginTop: "0.2rem",
  },
  stepStatusBadge: {
    fontSize: "0.72rem",
    fontWeight: 700,
    padding: "0.2rem 0.55rem",
    borderRadius: "6px",
  },
  stepCommitted: {
    background: "rgba(16, 185, 129, 0.15)",
    color: "#10b981",
  },
  stepCompensated: {
    background: "rgba(245, 158, 11, 0.15)",
    color: "#f59e0b",
  },
  stepFailed: {
    background: "rgba(239, 68, 68, 0.15)",
    color: "#ef4444",
  },
  latency: {
    fontSize: "0.75rem",
    color: "#64748b",
    marginTop: "0.2rem",
  },
  footer: {
    marginTop: "1.5rem",
    paddingTop: "1rem",
    borderTop: "1px solid #162032",
    display: "flex",
    justifyContent: "space-between",
    fontSize: "0.8rem",
    color: "#64748b",
  },
};

export default SagaTransactionTracker;
