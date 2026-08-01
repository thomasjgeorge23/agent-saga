"use client";

import React, { useEffect, useState } from "react";

export interface TrustNode {
  id: string;
  name: string;
  lat: number;
  lng: number;
  trust_score: number; // 0.0 to 1.0
  status: "VERIFIED" | "RISK_EVALUATED" | "BLOCKED";
}

export interface SpatialTrustRadarProps {
  nodes?: TrustNode[];
  radius_km?: number;
}

export const SpatialTrustRadar: React.FC<SpatialTrustRadarProps> = ({
  nodes = [
    { id: "node-1", name: "Campus Hub Node A", lat: 37.7749, lng: -122.4194, trust_score: 0.98, status: "VERIFIED" },
    { id: "node-2", name: "Merchant Terminal B", lat: 37.7752, lng: -122.418, trust_score: 0.91, status: "VERIFIED" },
    { id: "node-3", name: "High-Risk Flag C", lat: 37.7735, lng: -122.421, trust_score: 0.42, status: "RISK_EVALUATED" },
  ],
  radius_km = 5.0,
}) => {
  const [angle, setAngle] = useState<number>(0);

  useEffect(() => {
    const timer = setInterval(() => {
      setAngle((prev) => (prev + 3) % 360);
    }, 40);
    return () => clearInterval(timer);
  }, []);

  return (
    <div style={styles.card}>
      <header style={styles.header}>
        <div>
          <div style={styles.eyebrow}>SAGAOPS SPATIAL TRUST RADAR</div>
          <h3 style={styles.title}>Geospatial Safety Radar</h3>
        </div>
        <span style={styles.radiusTag}>Radius: {radius_km} km</span>
      </header>

      <div style={styles.radarBox}>
        {/* RADAR SWEEP LINE */}
        <div
          style={{
            ...styles.sweepLine,
            transform: `rotate(${angle}deg)`,
          }}
        />

        {/* CONCENTRIC RADAR RINGS */}
        <div style={{ ...styles.ring, width: "75%", height: "75%" }} />
        <div style={{ ...styles.ring, width: "50%", height: "50%" }} />
        <div style={{ ...styles.ring, width: "25%", height: "25%" }} />

        {/* NODES ON RADAR */}
        {nodes.map((node, i) => (
          <div
            key={node.id}
            style={{
              ...styles.nodeDot,
              left: `${30 + i * 25}%`,
              top: `${40 + (i % 2) * 20}%`,
              background: node.status === "VERIFIED" ? "#10b981" : "#f59e0b",
            }}
            title={`${node.name} - Trust: ${(node.trust_score * 100).toFixed(0)}%`}
          />
        ))}
      </div>

      <div style={styles.nodeList}>
        {nodes.map((n) => (
          <div key={n.id} style={styles.nodeRow}>
            <span>{n.name}</span>
            <b style={{ color: n.status === "VERIFIED" ? "#10b981" : "#f59e0b" }}>
              {(n.trust_score * 100).toFixed(0)}% TRUST
            </b>
          </div>
        ))}
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
    marginBottom: "1.2rem",
  },
  eyebrow: {
    fontFamily: "monospace",
    fontSize: "0.75rem",
    color: "#10b981",
    fontWeight: 700,
  },
  title: {
    fontSize: "1.2rem",
    fontWeight: 800,
    margin: "0.2rem 0 0",
  },
  radiusTag: {
    fontFamily: "monospace",
    fontSize: "0.75rem",
    background: "rgba(16, 185, 129, 0.15)",
    color: "#10b981",
    padding: "0.25rem 0.6rem",
    borderRadius: "6px",
  },
  radarBox: {
    position: "relative",
    width: "100%",
    height: "220px",
    background: "#050810",
    border: "1px solid #1e293b",
    borderRadius: "14px",
    overflow: "hidden",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    marginBottom: "1rem",
  },
  sweepLine: {
    position: "absolute",
    width: "50%",
    height: "2px",
    background: "linear-gradient(90deg, rgba(16, 185, 129, 0.8), transparent)",
    transformOrigin: "left center",
    left: "50%",
    top: "50%",
  },
  ring: {
    position: "absolute",
    borderRadius: "50%",
    border: "1px solid rgba(16, 185, 129, 0.2)",
    pointerEvents: "none",
  },
  nodeDot: {
    position: "absolute",
    width: "10px",
    height: "10px",
    borderRadius: "50%",
    boxShadow: "0 0 8px currentColor",
    cursor: "pointer",
  },
  nodeList: {
    display: "flex",
    flexDirection: "column",
    gap: "0.5rem",
    fontSize: "0.85rem",
  },
  nodeRow: {
    display: "flex",
    justifyContent: "space-between",
    background: "#0f172a",
    padding: "0.5rem 0.8rem",
    borderRadius: "6px",
    border: "1px solid #1e293b",
  },
};

export default SpatialTrustRadar;
