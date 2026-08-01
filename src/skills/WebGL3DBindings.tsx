"use client";

import React, { useEffect, useRef } from "react";

export interface WebGL3DBindingsProps {
  saga_status: "RUNNING" | "SUCCESS" | "ROLLED_BACK" | "HALTED";
}

export const WebGL3DBindings: React.FC<WebGL3DBindingsProps> = ({ saga_status }) => {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const gl = canvas.getContext("webgl");
    if (!gl) return;

    let animId: number;
    let width = (canvas.width = 400);
    let height = (canvas.height = 300);

    // Simple WebGL clear color based on status
    const updateClearColor = () => {
      if (saga_status === "SUCCESS") {
        gl.clearColor(0.06, 0.72, 0.5, 0.2);
      } else if (saga_status === "ROLLED_BACK") {
        gl.clearColor(0.96, 0.62, 0.04, 0.2);
      } else if (saga_status === "HALTED") {
        gl.clearColor(0.93, 0.26, 0.26, 0.2);
      } else {
        gl.clearColor(0.22, 0.74, 0.97, 0.2);
      }
    };

    const render = () => {
      updateClearColor();
      gl.clear(gl.COLOR_BUFFER_BIT);
      animId = requestAnimationFrame(render);
    };

    render();

    return () => {
      cancelAnimationFrame(animId);
    };
  }, [saga_status]);

  return (
    <div style={styles.container}>
      <div style={styles.badge}>3D WEBGL SHADER BINDINGS</div>
      <canvas ref={canvasRef} style={styles.canvas} />
      <div style={styles.statusText}>State: {saga_status}</div>
    </div>
  );
};

const styles: { [key: string]: React.CSSProperties } = {
  container: {
    background: "#050810",
    border: "1px solid #1e293b",
    borderRadius: "14px",
    padding: "1rem",
    textAlign: "center",
  },
  badge: {
    fontFamily: "monospace",
    fontSize: "0.75rem",
    color: "#a855f7",
    fontWeight: 700,
    marginBottom: "0.75rem",
  },
  canvas: {
    width: "100%",
    height: "180px",
    borderRadius: "10px",
    border: "1px solid #1e293b",
  },
  statusText: {
    marginTop: "0.5rem",
    fontFamily: "monospace",
    fontSize: "0.85rem",
    color: "#38bdf8",
  },
};

export default WebGL3DBindings;
