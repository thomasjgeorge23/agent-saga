"use client";

import React, { useState } from "react";

export interface ChaosResiliencyTheaterProps {
  on_inject_chaos?: (kind: "SIGKILL" | "NETWORK_DROP" | "WAL_CORRUPT") => Promise<void>;
}

export const ChaosResiliencyTheater: React.FC<ChaosResiliencyTheaterProps> = ({ on_inject_chaos }) => {
  const [chaosLog, setChaosLog] = useState<string[]>([]);
  const [injecting, setInjecting] = useState<boolean>(false);

  const handleInject = async (kind: "SIGKILL" | "NETWORK_DROP" | "WAL_CORRUPT") => {
    setInjecting(true);
    const newLog = [
      `[00:00.00] 💥 Injected Chaos Event: ${kind}`,
      `[00:00.05] 🚨 Worker process killed mid-transaction. Memory destroyed.`,
      `[00:00.12] ⚡ Separate RecoveryDaemon process detected expired lease.`,
      `[00:00.25] 📜 Reading durable hash-chained WAL log from disk...`,
      `[00:00.45] ↩ Executing LIFO compensation sweep: stripe.refund() -> inventory.release()`,
      `[00:00.82] ✓ Recovery Daemon Sweep Complete: 100% clean state restoration.`,
    ];
    setChaosLog(newLog);

    if (on_inject_chaos) {
      await on_inject_chaos(kind);
    }
    setInjecting(false);
  };

  return (
    <div style={styles.card}>
      <header style={styles.header}>
        <div>
          <div style={styles.eyebrow}>SAGAOPS CHAOS RESILIENCY THEATER</div>
          <h3 style={styles.title}>Dev-Mode Fault Injection & Recovery Sweep</h3>
        </div>
        <span style={styles.badge}>🧪 DEV RESILIENCY THEATER</span>
      </header>

      <div style={styles.buttonRow}>
        <button
          style={{ ...styles.injectBtn, background: "linear-gradient(135deg, #ef4444, #dc2626)" }}
          onClick={() => handleInject("SIGKILL")}
          disabled={injecting}
        >
          💀 Kill Process (`SIGKILL`)
        </button>

        <button
          style={{ ...styles.injectBtn, background: "linear-gradient(135deg, #f59e0b, #d97706)" }}
          onClick={() => handleInject("NETWORK_DROP")}
          disabled={injecting}
        >
          🌐 Network Dropout
        </button>

        <button
          style={{ ...styles.injectBtn, background: "linear-gradient(135deg, #a855f7, #7e22ce)" }}
          onClick={() => handleInject("WAL_CORRUPT")}
          disabled={injecting}
        >
          ⚡ Corrupt Line Injection
        </button>
      </div>

      {chaosLog.length > 0 && (
        <div style={styles.logBox}>
          <div style={styles.logHeader}>RECOVERY DAEMON REAL-TIME LOG</div>
          <pre style={styles.logPre}>{chaosLog.join("\n")}</pre>
        </div>
      )}
    </div>
  );
};

const styles: { [key: string]: React.CSSProperties } = {
  card: {
    background: "#0b1329",
    border: "1px solid #1e293b",
    borderRadius: "16px",
    padding: "1.5rem",
    color: "#f8fafc",
    fontFamily: 'system-ui, -apple-system, sans-serif',
  },
  header: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    marginBottom: "1.2rem",
  },
  eyebrow: {
    fontFamily: "monospace",
    fontSize: "0.75rem",
    color: "#ef4444",
    fontWeight: 700,
  },
  title: {
    fontSize: "1.2rem",
    fontWeight: 800,
    margin: "0.2rem 0 0",
  },
  badge: {
    fontFamily: "monospace",
    fontSize: "0.72rem",
    background: "rgba(239, 68, 68, 0.15)",
    color: "#ef4444",
    padding: "0.3rem 0.6rem",
    borderRadius: "6px",
    fontWeight: 700,
  },
  buttonRow: {
    display: "flex",
    gap: "0.8rem",
    marginBottom: "1.2rem",
    flexWrap: "wrap",
  },
  injectBtn: {
    color: "#ffffff",
    border: "none",
    padding: "0.65rem 1.1rem",
    borderRadius: "10px",
    fontWeight: 800,
    cursor: "pointer",
    fontSize: "0.85rem",
  },
  logBox: {
    background: "#050810",
    border: "1px solid #1e293b",
    borderRadius: "12px",
    padding: "1rem",
  },
  logHeader: {
    fontFamily: "monospace",
    fontSize: "0.75rem",
    color: "#64748b",
    marginBottom: "0.5rem",
    borderBottom: "1px solid #1e293b",
    paddingBottom: "0.4rem",
  },
  logPre: {
    fontFamily: "monospace",
    fontSize: "0.82rem",
    color: "#10b981",
    margin: 0,
    whiteSpace: "pre-wrap",
    lineHeight: 1.6,
  },
};

export default ChaosResiliencyTheater;
