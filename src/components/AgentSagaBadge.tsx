"use client";

import React from "react";

export interface AgentSagaBadgeProps {
  version?: string;
  wal_status?: "ACTIVE" | "FSYNC_ENABLED" | "BYOK_ENCRYPTED";
  show_byok_indicator?: boolean;
}

export const AgentSagaBadge: React.FC<AgentSagaBadgeProps> = ({
  version = "0.5.6",
  wal_status = "BYOK_ENCRYPTED",
  show_byok_indicator = true,
}) => {
  return (
    <div style={styles.badgeContainer}>
      {/* LIVE PULSE RING */}
      <span style={styles.pulseRing}>
        <span style={styles.pulseDot} />
      </span>

      <span style={styles.brandTitle}>
        agent-saga <span style={styles.versionTag}>v{version}</span>
      </span>

      <span style={styles.divider}>|</span>

      <span style={styles.walText}>
        ⚡ WAL: <b style={{ color: "#10b981" }}>{wal_status}</b>
      </span>

      {show_byok_indicator && (
        <span style={styles.byokPill} title="BYOK AES-128-CBC Fernet Encryption Active">
          🔐 BYOK
        </span>
      )}
    </div>
  );
};

const styles: { [key: string]: React.CSSProperties } = {
  badgeContainer: {
    display: "inline-flex",
    alignItems: "center",
    gap: "0.65rem",
    background: "rgba(15, 23, 42, 0.85)",
    backdropFilter: "blur(8px)",
    border: "1px solid rgba(255, 255, 255, 0.12)",
    padding: "0.45rem 1rem",
    borderRadius: "999px",
    color: "#f8fafc",
    fontFamily: 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif',
    fontSize: "0.85rem",
    boxShadow: "0 4px 20px rgba(0,0,0,0.3)",
    transition: "transform 0.2s ease, box-shadow 0.2s ease",
  },
  pulseRing: {
    position: "relative",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    width: "10px",
    height: "10px",
  },
  pulseDot: {
    width: "8px",
    height: "8px",
    borderRadius: "50%",
    background: "#10b981",
    boxShadow: "0 0 10px #10b981",
  },
  brandTitle: {
    fontWeight: 800,
    letterSpacing: "-0.01em",
  },
  versionTag: {
    fontFamily: "monospace",
    fontSize: "0.75rem",
    color: "#f59e0b",
    background: "rgba(245, 158, 11, 0.15)",
    padding: "0.1rem 0.4rem",
    borderRadius: "4px",
    marginLeft: "0.25rem",
  },
  divider: {
    color: "rgba(255, 255, 255, 0.2)",
  },
  walText: {
    fontSize: "0.8rem",
    color: "#94a3b8",
  },
  byokPill: {
    fontFamily: "monospace",
    fontSize: "0.7rem",
    fontWeight: 700,
    background: "rgba(56, 189, 248, 0.15)",
    color: "#38bdf8",
    border: "1px solid rgba(56, 189, 248, 0.3)",
    padding: "0.15rem 0.5rem",
    borderRadius: "6px",
  },
};

export default AgentSagaBadge;
