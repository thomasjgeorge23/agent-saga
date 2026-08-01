"use client";

import React, { useState } from "react";
import { AgentSagaBadge } from "./AgentSagaBadge";
import { SagaTransactionTracker, TransactionStep } from "./SagaTransactionTracker";
import { SagaVisualLedger, LedgerStep } from "./SagaVisualLedger";
import { UnseenAmbientMesh } from "./UnseenAmbientMesh";

export const UnseenSagaShowcase: React.FC = () => {
  const [sagaState, setSagaState] = useState<"IDLE" | "RUNNING" | "CLEAN_COMMIT" | "ROLLED_BACK">("CLEAN_COMMIT");

  // Sample transaction steps
  const trackerSteps: TransactionStep[] = [
    {
      id: "step-1",
      step_number: 1,
      tool_name: "stripe.charge",
      semantics: "COMPENSABLE",
      status: "COMMITTED",
      timestamp: "21:35:01.012",
      inverse_tool: "stripe.refund",
      latency_ms: 142,
    },
    {
      id: "step-2",
      step_number: 2,
      tool_name: "inventory.reserve_sku",
      semantics: "COMPENSABLE",
      status: "COMMITTED",
      timestamp: "21:35:01.184",
      inverse_tool: "inventory.release_sku",
      latency_ms: 88,
    },
    {
      id: "step-3",
      step_number: 3,
      tool_name: "logistics.create_shipment",
      semantics: "COMPENSABLE",
      status: "COMMITTED",
      timestamp: "21:35:01.310",
      inverse_tool: "logistics.cancel_shipment",
      latency_ms: 215,
    },
  ];

  const ledgerSteps: LedgerStep[] = [
    {
      seq: 101,
      tool: "stripe.charge",
      semantics: "COMPENSABLE",
      status: "COMMITTED",
      timestamp: "21:35:01.012",
      duration_ms: 142,
      payload_hash: "7f89ab103c8e5472190a6042189fbca9501a4e238120b39c0192e",
    },
    {
      seq: 102,
      tool: "inventory.reserve_sku",
      semantics: "COMPENSABLE",
      status: "COMMITTED",
      timestamp: "21:35:01.184",
      duration_ms: 88,
      payload_hash: "33baa49d10e85491a02f88e10b9817754d92a10",
    },
    {
      seq: 103,
      tool: "logistics.create_shipment",
      semantics: "COMPENSABLE",
      status: "COMMITTED",
      timestamp: "21:35:01.310",
      duration_ms: 215,
      payload_hash: "17d23dfe009b1178a9c010b981559a11789e02",
    },
  ];

  const triggerConfettiCelebration = () => {
    // Custom particle burst effect
    const count = 200;
    const defaults = { origin: { y: 0.7 } };

    function fire(particleRatio: number, opts: any) {
      if (typeof window !== "undefined" && (window as any).confetti) {
        (window as any).confetti({
          ...defaults,
          ...opts,
          particleCount: Math.floor(count * particleRatio),
        });
      }
    }

    fire(0.25, { spread: 26, startVelocity: 55 });
    fire(0.2, { spread: 60 });
    fire(0.35, { spread: 100, decay: 0.91, scalar: 0.8 });
    fire(0.1, { spread: 120, startVelocity: 25, decay: 0.92, scalar: 1.2 });
    fire(0.1, { spread: 120, startVelocity: 45 });
  };

  return (
    <div style={styles.showcaseWrapper}>
      {/* ORGANIC AMBIENT CANVAS MESH */}
      <UnseenAmbientMesh color1="#4f46e5" color2="#7c3aed" color3="#10b981" />

      {/* FOREGROUND CONTENT */}
      <div style={styles.container}>
        {/* HEADER BAR WITH AGENT SAGA BADGE */}
        <header style={styles.header}>
          <div>
            <div style={styles.eyebrow}>SAGAOPS SUPREME VISUAL SHOWCASE</div>
            <h1 style={styles.heroTitle}>
              Unseen-Grade <span style={{ color: "#38bdf8" }}>Kinetic Motion</span> & Transactional Trust
            </h1>
            <p style={styles.subtitle}>
              Published & Maintained by <b>SAGAOPS Enterprise</b> · Founded & Owned by <b>Thomas J George</b> (thomasjgeorge23@gmail.com)
            </p>
          </div>

          <AgentSagaBadge version="0.5.6" wal_status="BYOK_ENCRYPTED" show_byok_indicator={true} />
        </header>

        {/* ACTION CONTROL PANEL */}
        <div style={styles.actionPanel}>
          <button
            style={{
              ...styles.actionBtn,
              background: sagaState === "CLEAN_COMMIT" ? "linear-gradient(135deg, #10b981, #059669)" : "#1e293b",
            }}
            onClick={() => {
              setSagaState("CLEAN_COMMIT");
              triggerConfettiCelebration();
            }}
          >
            🎉 Simulate 100% Clean Commit
          </button>

          <button
            style={{
              ...styles.actionBtn,
              background: sagaState === "ROLLED_BACK" ? "linear-gradient(135deg, #f59e0b, #d97706)" : "#1e293b",
            }}
            onClick={() => setSagaState("ROLLED_BACK")}
          >
            🛡️ Simulate LIFO Compensation Rollback
          </button>
        </div>

        {/* DUAL COLUMN VISUAL COMPONENTS */}
        <div style={styles.grid}>
          {/* COLUMN 1: TRACKER */}
          <SagaTransactionTracker
            saga_id="tx_saga_99881102"
            saga_name="E-Commerce Multi-Step Order Processing"
            steps={trackerSteps}
            overall_status={sagaState === "ROLLED_BACK" ? "CLEAN_ROLLBACK" : "SUCCESS"}
            wal_durability="HASH_CHAINED_FSYNC"
            clean_lifo_compensation={sagaState === "ROLLED_BACK"}
          />

          {/* COLUMN 2: KINETIC VISUAL LEDGER */}
          <SagaVisualLedger
            saga_id="tx_saga_99881102"
            saga_name="WAL Real-Time Ledger Stream"
            steps={ledgerSteps}
            is_clean_finish={sagaState === "CLEAN_COMMIT"}
            byok_encrypted={true}
            on_trigger_confetti={triggerConfettiCelebration}
          />
        </div>
      </div>
    </div>
  );
};

const styles: { [key: string]: React.CSSProperties } = {
  showcaseWrapper: {
    position: "relative",
    minHeight: "100vh",
    background: "#050811",
    color: "#f8fafc",
    fontFamily: 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif',
    overflow: "hidden",
  },
  container: {
    position: "relative",
    zIndex: 1,
    maxWidth: "1400px",
    margin: "0 auto",
    padding: "3rem 2rem",
  },
  header: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    marginBottom: "2rem",
    borderBottom: "1px solid rgba(255, 255, 255, 0.08)",
    paddingBottom: "1.5rem",
    flexWrap: "wrap",
    gap: "1.5rem",
  },
  eyebrow: {
    fontFamily: "monospace",
    fontSize: "0.8rem",
    fontWeight: 700,
    color: "#a855f7",
    letterSpacing: "0.15em",
  },
  heroTitle: {
    fontSize: "2.2rem",
    fontWeight: 900,
    margin: "0.3rem 0",
  },
  subtitle: {
    color: "#94a3b8",
    fontSize: "0.95rem",
    margin: 0,
  },
  actionPanel: {
    display: "flex",
    gap: "1rem",
    marginBottom: "2.5rem",
  },
  actionBtn: {
    color: "#ffffff",
    border: "1px solid rgba(255, 255, 255, 0.15)",
    padding: "0.75rem 1.4rem",
    borderRadius: "12px",
    fontWeight: 800,
    cursor: "pointer",
    fontSize: "0.9rem",
    boxShadow: "0 10px 25px rgba(0, 0, 0, 0.3)",
    transition: "transform 0.15s ease",
  },
  grid: {
    display: "grid",
    gridTemplateColumns: "repeat(auto-fit, minmax(580px, 1fr))",
    gap: "2rem",
  },
};

export default UnseenSagaShowcase;
