"use client";

import React, { useEffect, useRef } from "react";

export interface UnseenAmbientMeshProps {
  color1?: string;
  color2?: string;
  color3?: string;
  interactive?: boolean;
}

export const UnseenAmbientMesh: React.FC<UnseenAmbientMeshProps> = ({
  color1 = "#4f46e5",
  color2 = "#7c3aed",
  color3 = "#10b981",
  interactive = true,
}) => {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    let animationFrameId: number;
    let width = (canvas.width = window.innerWidth);
    let height = (canvas.height = window.innerHeight);

    const handleResize = () => {
      if (!canvas) return;
      width = canvas.width = window.innerWidth;
      height = canvas.height = window.innerHeight;
    };
    window.addEventListener("resize", handleResize);

    // Mouse positions for Lerp physics
    const mouse = { x: width / 2, y: height / 2, targetX: width / 2, targetY: height / 2 };

    const handleMouseMove = (e: MouseEvent) => {
      if (!interactive) return;
      mouse.targetX = e.clientX;
      mouse.targetY = e.clientY;
    };
    window.addEventListener("mousemove", handleMouseMove);

    // Organic nodes for ambient mesh
    const numNodes = 24;
    const nodes = Array.from({ length: numNodes }, () => ({
      x: Math.random() * width,
      y: Math.random() * height,
      vx: (Math.random() - 0.5) * 0.6,
      vy: (Math.random() - 0.5) * 0.6,
      radius: Math.random() * 120 + 80,
      color: [color1, color2, color3][Math.floor(Math.random() * 3)],
    }));

    const render = () => {
      // Lerp mouse towards target for fluid organic motion
      mouse.x += (mouse.targetX - mouse.x) * 0.05;
      mouse.y += (mouse.targetY - mouse.y) * 0.05;

      ctx.clearRect(0, 0, width, height);

      // Render glowing mesh nodes
      nodes.forEach((node, i) => {
        node.x += node.vx;
        node.y += node.vy;

        if (node.x < -100) node.x = width + 100;
        if (node.x > width + 100) node.x = -100;
        if (node.y < -100) node.y = height + 100;
        if (node.y > height + 100) node.y = -100;

        // Apply slight attraction to lerped mouse
        if (interactive) {
          const dx = mouse.x - node.x;
          const dy = mouse.y - node.y;
          const dist = Math.sqrt(dx * dx + dy * dy);
          if (dist < 300) {
            node.x += (dx / dist) * 0.2;
            node.y += (dy / dist) * 0.2;
          }
        }

        const gradient = ctx.createRadialGradient(node.x, node.y, 0, node.x, node.y, node.radius * 1.5);
        gradient.addColorStop(0, node.color + "33"); // 20% opacity
        gradient.addColorStop(0.5, node.color + "11"); // 7% opacity
        gradient.addColorStop(1, "transparent");

        ctx.fillStyle = gradient;
        ctx.beginPath();
        ctx.arc(node.x, node.y, node.radius * 1.5, 0, Math.PI * 2);
        ctx.fill();
      });

      // Render connecting fluid lines between close nodes
      for (let i = 0; i < nodes.length; i++) {
        for (let j = i + 1; j < nodes.length; j++) {
          const dx = nodes[i].x - nodes[j].x;
          const dy = nodes[i].y - nodes[j].y;
          const dist = Math.sqrt(dx * dx + dy * dy);
          if (dist < 220) {
            ctx.strokeStyle = `rgba(255, 255, 255, ${0.08 * (1 - dist / 220)})`;
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(nodes[i].x, nodes[i].y);
            ctx.lineTo(nodes[j].x, nodes[j].y);
            ctx.stroke();
          }
        }
      }

      animationFrameId = requestAnimationFrame(render);
    };

    render();

    return () => {
      window.removeEventListener("resize", handleResize);
      window.removeEventListener("mousemove", handleMouseMove);
      cancelAnimationFrame(animationFrameId);
    };
  }, [color1, color2, color3, interactive]);

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

export default UnseenAmbientMesh;
