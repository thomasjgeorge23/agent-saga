"use client";

import React, { useState } from "react";

export interface AlternativeOption {
  id: string;
  name: string;
  price: string;
  rating: string;
  reason: string;
}

export interface SelfHealingUIProps {
  failed_tool: string;
  failure_reason: string;
  alternatives?: AlternativeOption[];
  on_select_alternative?: (alt: AlternativeOption) => Promise<void>;
}

export const SelfHealingUI: React.FC<SelfHealingUIProps> = ({
  failed_tool = "inventory.reserve_sku",
  failure_reason = "Out of stock at Primary Warehouse #4",
  alternatives = [
    { id: "alt-1", name: "Fulfill via Nearby Express Hub #2", price: "$149.00", rating: "4.9 ★", reason: "Identical SKU in stock 2 miles away" },
    { id: "alt-2", name: "Fulfill via Regional Depot #9", price: "$145.00", rating: "4.8 ★", reason: "Bulk stock available with standard delivery" },
  ],
  on_select_alternative,
}) => {
  const [retryingId, setRetryingId] = useState<string | null>(null);
  const [healedMessage, setHealedMessage] = useState<string | null>(null);

  const handleRetry = async (alt: AlternativeOption) => {
    setRetryingId(alt.id);
    setHealedMessage(`⌛ Retrying saga execution via ${alt.name}...`);
    try {
      if (on_select_alternative) {
        await on_select_alternative(alt);
      }
      setTimeout(() => {
        setHealedMessage(`✓ Self-Healing Complete: Order rerouted & WAL committed via ${alt.name}`);
        setRetryingId(null);
      }, 1000);
    } catch (err) {
      setHealedMessage(`⚠ Compensation reroute failed. Human operator alerted.`);
      setRetryingId(null);
    }
  };

  return (
    <div style={styles.card}>
      <div style={styles.alertHeader}>
        <span style={{ fontSize: "1.3rem" }}>🛡️</span>
        <div>
          <b style={{ color: "#f59e0b" }}>SAGAOPS Self-Healing UI Triggered</b>
          <p style={{ margin: "0.15rem 0 0", fontSize: "0.85rem", opacity: 0.9 }}>
            Step <code>{failed_tool}</code> rolled back cleanly. Autonomous AI prompt generated 2 instant alternatives:
          </p>
        </div>
      </div>

      <div style={styles.altList}>
        {alternatives.map((alt) => (
          <div key={alt.id} style={styles.altCard}>
            <div>
              <b style={{ fontSize: "0.95rem" }}>{alt.name}</b>
              <div style={styles.reasonText}>{alt.reason}</div>
              <div style={styles.metaRow}>
                <span>Price: {alt.price}</span> · <span>Rating: {alt.rating}</span>
              </div>
            </div>

            <button
              style={styles.retryBtn}
              onClick={() => handleRetry(alt)}
              disabled={retryingId === alt.id}
            >
              {retryingId === alt.id ? "Retrying..." : "1-Click Heal →"}
            </button>
          </div>
        ))}
      </div>

      {healedMessage && <div style={styles.healedBox}>{healedMessage}</div>}
    </div>
  );
};

const styles: { [key: string]: React.CSSProperties } = {
  card: {
    background: "#0b1329",
    border: "1px solid rgba(245, 158, 11, 0.4)",
    borderRadius: "16px",
    padding: "1.5rem",
    color: "#f8fafc",
    fontFamily: 'system-ui, -apple-system, sans-serif',
  },
  alertHeader: {
    display: "flex",
    gap: "0.8rem",
    alignItems: "flex-start",
    borderBottom: "1px solid #1e293b",
    paddingBottom: "1rem",
    marginBottom: "1.2rem",
  },
  altList: {
    display: "flex",
    flexDirection: "column",
    gap: "0.85rem",
  },
  altCard: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    background: "#0f172a",
    border: "1px solid #1e293b",
    borderRadius: "12px",
    padding: "1rem 1.2rem",
  },
  reasonText: {
    fontSize: "0.82rem",
    color: "#38bdf8",
    marginTop: "0.2rem",
  },
  metaRow: {
    fontSize: "0.78rem",
    color: "#64748b",
    marginTop: "0.2rem",
  },
  retryBtn: {
    background: "linear-gradient(135deg, #10b981, #059669)",
    color: "#070c16",
    border: "none",
    padding: "0.6rem 1.1rem",
    borderRadius: "8px",
    fontWeight: 800,
    cursor: "pointer",
    fontSize: "0.85rem",
  },
  healedBox: {
    marginTop: "1rem",
    padding: "0.8rem",
    background: "#050810",
    border: "1px solid #10b981",
    borderRadius: "8px",
    fontFamily: "monospace",
    fontSize: "0.85rem",
    color: "#10b981",
  },
};

export default SelfHealingUI;
