"use client";

import React, { useEffect, useRef } from "react";

export interface LiquidMeshEngineProps {
  transaction_volume?: number; // Shifts mesh turbulence & speed dynamically
  system_health?: "HEALTHY" | "DEGRADED" | "CRITICAL";
}

export const LiquidMeshEngine: React.FC<LiquidMeshEngineProps> = ({
  transaction_volume = 42,
  system_health = "HEALTHY",
}) => {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    let animId: number;
    let w = (canvas.width = window.innerWidth);
    let h = (canvas.height = window.innerHeight);

    const onResize = () => {
      if (!canvas) return;
      w = canvas.width = window.innerWidth;
      h = canvas.height = window.innerHeight;
    };
    window.addEventListener("resize", onResize);

    const baseColor =
      system_health === "HEALTHY"
        ? "#10b981"
        : system_health === "DEGRADED"
        ? "#f59e0b"
        : "#ef4444";

    const speedMultiplier = Math.min(3.0, 1.0 + transaction_volume / 50);

    const particles = Array.from({ length: 30 }, () => ({
      x: Math.random() * w,
      y: Math.random() * h,
      vx: (Math.random() - 0.5) * 1.2 * speedMultiplier,
      vy: (Math.random() - 0.5) * 1.2 * speedMultiplier,
      radius: Math.random() * 140 + 70,
    }));

    const render = () => {
      ctx.clearRect(0, 0, w, h);

      particles.forEach((p) => {
        p.x += p.vx;
        p.y += p.vy;

        if (p.x < -100) p.x = w + 100;
        if (p.x > w + 100) p.x = -100;
        if (p.y < -100) p.y = h + 100;
        if (p.y > h + 100) p.y = -100;

        const grad = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, p.radius);
        grad.addColorStop(0, baseColor + "33");
        grad.addColorStop(0.6, baseColor + "0d");
        grad.addColorStop(1, "transparent");

        ctx.fillStyle = grad;
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.radius, 0, Math.PI * 2);
        ctx.fill();
      });

      animId = requestAnimationFrame(render);
    };

    render();

    return () => {
      window.removeEventListener("resize", onResize);
      cancelAnimationFrame(animId);
    };
  }, [transaction_volume, system_health]);

  return (
    <canvas
      ref={canvasRef}
      style={{
        position: "fixed",
        top: 0,
        left: 0,
        width: "100vw",
        height: "100vh",
        pointerEvents: "none",
        zIndex: 0,
      }}
    />
  );
};

export default LiquidMeshEngine;
