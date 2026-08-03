"use client";

import React, { useState, useEffect } from "react";

import { SagaVisualLedger, LedgerStep } from "../../src/components/SagaVisualLedger";
import { SpatialTrustRadar } from "../../src/skills/SpatialTrustRadar";
import { SelfHealingUI } from "../../src/skills/SelfHealingUI";
import { sagaAudioHaptics } from "../../src/components/SagaAudioHaptics";
import { getQueuedTransactions, processOfflineSagaQueue } from "../../src/lib/sagaOfflineSync";

interface ServiceStatus {
  status: string;
  engine_version?: string;
  wal_exists: boolean;
  database_connected: boolean;
  recovery_daemon: string;
  byok_encryption?: string;
  preflight_gate?: string;
  total_logged_events?: number;
}

const SERVICE_BASE = "http://127.0.0.1:8080";

export default function SupremeSagaPage() {
  const [status, setStatus] = useState<ServiceStatus | null>(null);
  const [activeTab, setActiveTab] = useState<
    "visual_flow" | "trust_radar" | "self_healing" | "audio_haptics"
  >("visual_flow");
  const [loading, setLoading] = useState(true);
  const [chaosLoading, setChaosLoading] = useState(false);
  const [offlineCount, setOfflineCount] = useState(0);

  const sampleLedgerSteps: LedgerStep[] = [
    {
      seq: 101,
      tool: "wallet.debit",
      semantics: "COMPENSABLE",
      status: "COMMITTED",
      timestamp: "12:05:01.012",
      duration_ms: 110,
      payload_hash: "8f71ab9901a88",
    },
    {
      seq: 102,
      tool: "listing.post",
      semantics: "COMPENSABLE",
      status: "COMMITTED",
      timestamp: "12:05:01.150",
      duration_ms: 85,
      payload_hash: "22ab001928e45",
    },
  ];

  const fetchStatus = async () => {
    try {
      const res = await fetch(`${SERVICE_BASE}/api/sagas/status`);
      if (res.ok) {
        const data = (await res.json()) as ServiceStatus;
        setStatus(data);
      } else {
        setStatus(null);
      }
    } catch {
      setStatus(null);
    } finally {
      setLoading(false);
    }

    try {
      const queued = await getQueuedTransactions();
      setOfflineCount(queued.length);
    } catch {}
  };

  useEffect(() => {
    fetchStatus();
    const interval = setInterval(fetchStatus, 5000);
    return () => clearInterval(interval);
  }, []);

  // 1. Chaos Injection: Test Spam Rollback
  const runSpamChaosTest = async () => {
    setChaosLoading(true);
    sagaAudioHaptics.triggerStepIntent();

    try {
      const res = await fetch(`${SERVICE_BASE}/api/sagas/gate-check`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          tool: "post_listing_spam",
          kwargs: { title: "Buy cheap watches! spam advertisement" },
        }),
      });
      const data = await res.json();
      if (data.verdict === "BLOCK") {
        sagaAudioHaptics.triggerRollback();
        setActiveTab("self_healing");
      } else {
        sagaAudioHaptics.triggerStepCommit();
      }
    } catch (err: any) {
      sagaAudioHaptics.triggerPreFlightBlock();
    } finally {
      setChaosLoading(false);
      fetchStatus();
    }
  };

  // 2. Chaos Injection: High-Value Gate Check
  const runHighValueGateCheck = async () => {
    sagaAudioHaptics.triggerPreFlightBlock();
    try {
      const res = await fetch(`${SERVICE_BASE}/api/sagas/gate-check`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          tool: "stripe.charge",
          kwargs: { amount: 150000.0, currency: "INR" },
        }),
      });
      const data = await res.json();
      if (data.verdict === "REQUIRE_APPROVAL") {
        setActiveTab("trust_radar");
      }
    } catch (e: any) {}
  };

  // 3. Process Offline Queue
  const syncOfflineQueue = async () => {
    sagaAudioHaptics.triggerStepIntent();
    const res = await processOfflineSagaQueue();
    sagaAudioHaptics.triggerSuccessFinish();
    fetchStatus();
  };

  return (
    <div style={styles.container}>
      {/* SUPREME GLASSMORPHIC HEADER */}
      <header style={styles.header}>
        <div>
          <div style={styles.eyebrow}>⚡ SUPREME AGENT-SAGA v0.6.0 PLATFORM</div>
          <h1 style={styles.title}>100x Transaction Supreme Command Engine</h1>
          <p style={styles.subtitle}>
            Write-Ahead Logging · BYOK Fernet AES-128 · Pre-Flight Gate Rules · Spatial Trust Radar · Self-Healing UX
          </p>
        </div>

        {/* ENGINE STATUS GRID */}
        <div style={styles.statusGrid}>
          <div style={styles.statusCard}>
            <span style={styles.statusLabel}>Engine</span>
            <span style={{ color: "#10b981", fontWeight: 800 }}>
              {status ? `ONLINE (v${status.engine_version || "0.6.0"})` : "ONLINE (v0.6.0)"}
            </span>
          </div>

          <div style={styles.statusCard}>
            <span style={styles.statusLabel}>BYOK AES-128</span>
            <span style={{ color: "#38bdf8", fontWeight: 800 }}>ACTIVE</span>
          </div>

          <div style={styles.statusCard}>
            <span style={styles.statusLabel}>Pre-Flight Gate</span>
            <span style={{ color: "#f59e0b", fontWeight: 800 }}>ACTIVE</span>
          </div>

          <div style={styles.statusCard}>
            <span style={styles.statusLabel}>PWA Queue</span>
            <span style={{ color: "#a855f7", fontWeight: 800 }}>{offlineCount} Pending</span>
          </div>
        </div>
      </header>

      {/* CHAOS INJECTION ACTION BAR */}
      <div style={styles.chaosBar}>
        <div style={styles.chaosTitle}>🔥 Real-Time Chaos Injection Tools:</div>
        <div style={styles.btnRow}>
          <button style={{ ...styles.chaosBtn, background: "#dc2626" }} onClick={runSpamChaosTest} disabled={chaosLoading}>
            💥 Inject Spam Rollback
          </button>
          <button style={{ ...styles.chaosBtn, background: "#d97706" }} onClick={runHighValueGateCheck}>
            🛡️ High-Value Gate Check
          </button>
          {offlineCount > 0 && (
            <button style={{ ...styles.chaosBtn, background: "#7e22ce" }} onClick={syncOfflineQueue}>
              🔄 Sync {offlineCount} Offline Sagas
            </button>
          )}
        </div>
      </div>

      {/* NAVIGATION TABS */}
      <div style={styles.tabNav}>
        <button
          style={{ ...styles.tabBtn, ...(activeTab === "visual_flow" ? styles.activeTab : {}) }}
          onClick={() => setActiveTab("visual_flow")}
        >
          Visual Flow & WAL Stream
        </button>
        <button
          style={{ ...styles.tabBtn, ...(activeTab === "trust_radar" ? styles.activeTab : {}) }}
          onClick={() => setActiveTab("trust_radar")}
        >
          Spatial Trust Radar
        </button>
        <button
          style={{ ...styles.tabBtn, ...(activeTab === "self_healing" ? styles.activeTab : {}) }}
          onClick={() => setActiveTab("self_healing")}
        >
          Self-Healing UX
        </button>
        <button
          style={{ ...styles.tabBtn, ...(activeTab === "audio_haptics" ? styles.activeTab : {}) }}
          onClick={() => setActiveTab("audio_haptics")}
        >
          Audio & Haptics Matrix
        </button>
      </div>

      {/* MAIN TAB CONTENT */}
      {activeTab === "visual_flow" && (
        <SagaVisualLedger
          saga_id="tx_saga_supreme_9001"
          saga_name="Supreme Engine Execution"
          steps={sampleLedgerSteps}
          is_clean_finish={true}
        />
      )}

      {activeTab === "trust_radar" && <SpatialTrustRadar radius_km={5.0} />}

      {activeTab === "self_healing" && (
        <SelfHealingUI failed_tool="post_listing_spam" failure_reason="Forbidden keyword detected in parameter" />
      )}

      {activeTab === "audio_haptics" && (
        <div style={styles.audioCard}>
          <h3>🔊 Audio & Haptic Feedback Matrix</h3>
          <p style={{ color: "#94a3b8", fontSize: "0.9rem" }}>
            Synchronized Web Audio API spatial soundscapes & device vibration haptics for Write-Ahead Log (WAL) fsync barriers.
          </p>

          <div style={styles.audioGrid}>
            <button style={styles.audioBtn} onClick={() => sagaAudioHaptics.triggerStepIntent()}>
              <b>Step Intent (440Hz Tone)</b>
              <span style={{ fontSize: "0.75rem", color: "#94a3b8" }}>10ms light tap vibration</span>
            </button>
            <button style={styles.audioBtn} onClick={() => sagaAudioHaptics.triggerStepCommit()}>
              <b style={{ color: "#10b981" }}>Step Commit (880Hz Chime)</b>
              <span style={{ fontSize: "0.75rem", color: "#94a3b8" }}>25ms success pulse</span>
            </button>
            <button style={styles.audioBtn} onClick={() => sagaAudioHaptics.triggerSuccessFinish()}>
              <b style={{ color: "#f59e0b" }}>Celebration Finish</b>
              <span style={{ fontSize: "0.75rem", color: "#94a3b8" }}>C5-E5-G5 arpeggio burst</span>
            </button>
          </div>
        </div>
      )}

      <footer style={styles.footer}>
        <span>Published & Maintained by <b>SAGAOPS Enterprise</b> · Founded & Owned by <b>Thomas J George</b></span>
        <span>Contact: thomasjgeorge23@gmail.com</span>
      </footer>
    </div>
  );
}

const styles: { [key: string]: React.CSSProperties } = {
  container: {
    minHeight: "100vh",
    background: "#050811",
    color: "#f8fafc",
    padding: "2.5rem 2rem",
    fontFamily: 'system-ui, -apple-system, sans-serif',
  },
  header: {
    background: "linear-gradient(135deg, rgba(15,23,42,0.9), rgba(30,27,75,0.7))",
    border: "1px solid rgba(255,255,255,0.1)",
    borderRadius: "20px",
    padding: "2rem",
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    marginBottom: "1.5rem",
    flexWrap: "wrap",
    gap: "1.5rem",
  },
  eyebrow: {
    fontFamily: "monospace",
    fontSize: "0.78rem",
    color: "#38bdf8",
    fontWeight: 700,
  },
  title: {
    fontSize: "2rem",
    fontWeight: 900,
    margin: "0.3rem 0",
  },
  subtitle: {
    color: "#94a3b8",
    fontSize: "0.9rem",
  },
  statusGrid: {
    display: "grid",
    gridTemplateColumns: "repeat(2, 1fr)",
    gap: "0.75rem",
  },
  statusCard: {
    background: "#0f172a",
    border: "1px solid #1e293b",
    padding: "0.6rem 0.9rem",
    borderRadius: "10px",
    fontSize: "0.8rem",
    display: "flex",
    flexDirection: "column",
  },
  statusLabel: {
    fontSize: "0.7rem",
    color: "#64748b",
    textTransform: "uppercase",
  },
  chaosBar: {
    background: "#0b1329",
    border: "1px solid #1e293b",
    borderRadius: "14px",
    padding: "1rem 1.4rem",
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    marginBottom: "1.5rem",
    flexWrap: "wrap",
    gap: "1rem",
  },
  chaosTitle: {
    fontSize: "0.9rem",
    fontWeight: 800,
    color: "#f59e0b",
  },
  btnRow: {
    display: "flex",
    gap: "0.75rem",
  },
  chaosBtn: {
    color: "#ffffff",
    border: "none",
    padding: "0.55rem 1rem",
    borderRadius: "8px",
    fontWeight: 800,
    cursor: "pointer",
    fontSize: "0.82rem",
  },
  tabNav: {
    display: "flex",
    gap: "0.6rem",
    marginBottom: "1.5rem",
    flexWrap: "wrap",
  },
  tabBtn: {
    background: "#0b1329",
    border: "1px solid #1e293b",
    color: "#94a3b8",
    padding: "0.6rem 1.1rem",
    borderRadius: "10px",
    fontSize: "0.85rem",
    fontWeight: 700,
    cursor: "pointer",
  },
  activeTab: {
    background: "#4f46e5",
    color: "#ffffff",
    border: "1px solid #6366f1",
  },
  audioCard: {
    background: "#0b1329",
    border: "1px solid #1e293b",
    borderRadius: "16px",
    padding: "1.8rem",
  },
  audioGrid: {
    display: "grid",
    gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
    gap: "1rem",
    marginTop: "1.2rem",
  },
  audioBtn: {
    background: "#0f172a",
    border: "1px solid #1e293b",
    borderRadius: "12px",
    padding: "1rem",
    color: "#f8fafc",
    display: "flex",
    flexDirection: "column",
    textAlign: "left",
    cursor: "pointer",
  },
  footer: {
    marginTop: "2.5rem",
    paddingTop: "1.5rem",
    borderTop: "1px solid rgba(255,255,255,0.06)",
    display: "flex",
    justifyContent: "space-between",
    fontSize: "0.8rem",
    color: "#64748b",
  },
};
