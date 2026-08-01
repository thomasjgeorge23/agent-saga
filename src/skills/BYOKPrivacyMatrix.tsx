"use client";

import React, { useState } from "react";

export interface BYOKPrivacyMatrixProps {
  key_source?: string;
  cipher_algorithm?: string;
  sample_plaintext?: string;
}

export const BYOKPrivacyMatrix: React.FC<BYOKPrivacyMatrixProps> = ({
  key_source = "AGENT_SAGA_WAL_KEY",
  cipher_algorithm = "AES-128-CBC-Fernet-HMAC-SHA256",
  sample_plaintext = '{"amount": 9500, "customer": "usr_9988", "notes": "Confidential Enterprise Settlement"}',
}) => {
  const [encrypted, setEncrypted] = useState<boolean>(true);

  const ciphertext =
    "E1:gAAAAABnZ5X8yT9a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0u1v2w3x4y5z6a7b8c9d0e1f2g3h4i5j6k7l8m9n0o1p2q3r4s5t6u7v8w9x0y1z2a3b4c5d6e7f8g9h0";

  return (
    <div style={styles.card}>
      <header style={styles.header}>
        <div>
          <div style={styles.eyebrow}>SAGAOPS BYOK PRIVACY MATRIX</div>
          <h3 style={styles.title}>Zero-Knowledge Encryption Audit</h3>
        </div>
        <span style={styles.badge}>🔐 HARDWARE-GRADE PROTECTION</span>
      </header>

      <div style={styles.infoRow}>
        <span>Key Source: <code>{key_source}</code></span>
        <span>Algorithm: <code>{cipher_algorithm}</code></span>
      </div>

      <div style={styles.matrixBox}>
        <div style={styles.matrixHeader}>
          <span>{encrypted ? "WAL RECORD ON DISK (CIPHERTEXT)" : "DECRYPTED IN-MEMORY VIEW"}</span>
          <button style={styles.toggleBtn} onClick={() => setEncrypted(!encrypted)}>
            {encrypted ? "Show Decrypted Payload" : "Show Encrypted Ciphertext"}
          </button>
        </div>

        <pre style={styles.preContent}>{encrypted ? ciphertext : sample_plaintext}</pre>
      </div>

      <div style={styles.auditFooter}>
        <span>✓ HMAC-SHA256 Signed</span> · <span>✓ Zero Plaintext PII at Rest</span> · <span>✓ Tamper-Evident</span>
      </div>
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
    marginBottom: "1rem",
  },
  eyebrow: {
    fontFamily: "monospace",
    fontSize: "0.75rem",
    color: "#38bdf8",
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
    background: "rgba(56, 189, 248, 0.15)",
    color: "#38bdf8",
    padding: "0.3rem 0.6rem",
    borderRadius: "6px",
    fontWeight: 700,
  },
  infoRow: {
    display: "flex",
    gap: "1.5rem",
    fontSize: "0.82rem",
    color: "#94a3b8",
    marginBottom: "1rem",
  },
  matrixBox: {
    background: "#050810",
    border: "1px solid #1e293b",
    borderRadius: "12px",
    padding: "1rem",
  },
  matrixHeader: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    fontFamily: "monospace",
    fontSize: "0.75rem",
    color: "#64748b",
    marginBottom: "0.75rem",
  },
  toggleBtn: {
    background: "#1e293b",
    color: "#f8fafc",
    border: "none",
    padding: "0.3rem 0.6rem",
    borderRadius: "6px",
    cursor: "pointer",
    fontSize: "0.75rem",
  },
  preContent: {
    fontFamily: "monospace",
    fontSize: "0.82rem",
    color: "#10b981",
    margin: 0,
    whiteSpace: "pre-wrap",
    wordBreak: "break-all",
  },
  auditFooter: {
    marginTop: "1rem",
    fontSize: "0.8rem",
    color: "#64748b",
    textAlign: "center",
  },
};

export default BYOKPrivacyMatrix;
