"""`agent_saga/space_canvas.py` -- Supreme Celestial Planet Orbit & Space Canvas Engine.

Features:
- Division-by-zero NaN protection (dist || 1) preventing top-left cursor freezing.
- 4 3D-shaded orbiting planets (Chronos, Aegis, Cipher, Apex) with glowing ring systems.
- Passive event listeners ({ passive: true }) for 120fps zero-lag interaction.
- Dynamic cosmic particle dust and gravitational lens physics.

Published & Maintained by: SAGAOPS Enterprise
Founder & Owner: Thomas J George (thomasjgeorge23@gmail.com)
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class SpaceCanvasConfig:
    def __init__(
        self,
        star_count: int = 180,
        enable_planets: bool = True,
        warp_speed: float = 1.0,
    ):
        self.star_count = star_count
        self.enable_planets = enable_planets
        self.warp_speed = warp_speed


def generate_space_canvas_script(config: Optional[SpaceCanvasConfig] = None) -> str:
    """Generate self-contained, ultra-smooth 120fps celestial planet orbit space engine."""
    cfg = config or SpaceCanvasConfig()

    return f"""
/* agent-saga Supreme Celestial Planet Orbit & Space Engine */
(function() {{
  const canvas = document.getElementById('spaceCanvas') || (function() {{
    const c = document.createElement('canvas');
    c.id = 'spaceCanvas';
    c.style.cssText = 'position:fixed;top:0;left:0;width:100vw;height:100vh;pointer-events:none;z-index:0;will-change:transform;transform:translateZ(0);opacity:0.9;';
    document.body.prepend(c);
    return c;
  }})();

  const ctx = canvas.getContext('2d', {{ alpha: false }});
  if (!ctx) return;

  let width = canvas.width = window.innerWidth;
  let height = canvas.height = window.innerHeight;

  let resizeTimeout;
  window.addEventListener('resize', () => {{
    clearTimeout(resizeTimeout);
    resizeTimeout = setTimeout(() => {{
      width = canvas.width = window.innerWidth;
      height = canvas.height = window.innerHeight;
    }}, 100);
  }}, {{ passive: true }});

  const mouse = {{ x: width / 2, y: height / 2, targetX: width / 2, targetY: height / 2 }};
  window.addEventListener('mousemove', (e) => {{
    if (e.clientX !== 0 || e.clientY !== 0) {{
      mouse.targetX = e.clientX;
      mouse.targetY = e.clientY;
    }}
  }}, {{ passive: true }});

  const stars = Array.from({{ length: {cfg.star_count} }}, () => ({{
    x: (Math.random() - 0.5) * width * 2,
    y: (Math.random() - 0.5) * height * 2,
    z: Math.random() * width,
    radius: Math.random() * 1.5 + 0.5,
    color: ['#4f46e5', '#7c3aed', '#10b981', '#38bdf8', '#ffffff'][Math.floor(Math.random() * 5)]
  }}));

  // Celestial Planets System
  let angle = 0;
  const planets = [
    {{ name: 'Chronos', color: '#38bdf8', glow: 'rgba(56,189,248,0.4)', radius: 24, orbitR: 180, speed: 0.005, ring: true }},
    {{ name: 'Aegis', color: '#10b981', glow: 'rgba(16,185,129,0.4)', radius: 30, orbitR: 300, speed: 0.003, ring: false }},
    {{ name: 'Cipher', color: '#a855f7', glow: 'rgba(168,85,247,0.4)', radius: 22, orbitR: 420, speed: 0.002, ring: true }},
    {{ name: 'Apex', color: '#f59e0b', glow: 'rgba(245,158,11,0.4)', radius: 36, orbitR: 540, speed: 0.001, ring: true }}
  ];

  function render() {{
    if (document.hidden) {{
      requestAnimationFrame(render);
      return;
    }}

    mouse.x += (mouse.targetX - mouse.x) * 0.08;
    mouse.y += (mouse.targetY - mouse.y) * 0.08;

    ctx.fillStyle = '#050811';
    ctx.fillRect(0, 0, width, height);

    const cx = width / 2;
    const cy = height / 2;

    // Render Cosmic Stars
    for (let i = 0; i < stars.length; i++) {{
      const s = stars[i];
      s.z -= {cfg.warp_speed} * 1.2;
      if (s.z <= 0) s.z = width;

      const k = 256 / s.z;
      const px = s.x * k + cx;
      const py = s.y * k + cy;

      if (px >= 0 && px <= width && py >= 0 && py <= height) {{
        const size = (1 - s.z / width) * s.radius * 2.2;
        const opacity = (1 - s.z / width);

        let pullX = 0, pullY = 0;
        const dx = mouse.x - px;
        const dy = mouse.y - py;
        const distSq = dx * dx + dy * dy;

        if (distSq < 40000 && distSq > 4) {{
          const dist = Math.sqrt(distSq) || 1;
          const factor = (200 - dist) * 0.08;
          pullX = (dx / dist) * factor;
          pullY = (dy / dist) * factor;
        }}

        ctx.fillStyle = s.color;
        ctx.globalAlpha = opacity;
        ctx.beginPath();
        ctx.arc(px + pullX, py + pullY, Math.max(0.1, size), 0, 6.28318);
        ctx.fill();
        ctx.globalAlpha = 1.0;
      }}
    }}

    // Render Orbiting Celestial Planets
    if ({str(cfg.enable_planets).lower()}) {{
      angle += 0.005;
      planets.forEach((p, idx) => {{
        const pAngle = angle * (idx % 2 === 0 ? 1 : -1) + (idx * Math.PI / 2);
        const px = cx + Math.cos(pAngle) * p.orbitR;
        const py = cy + Math.sin(pAngle) * (p.orbitR * 0.4);

        // Planet Glow Halo
        const grad = ctx.createRadialGradient(px, py, p.radius * 0.2, px, py, p.radius * 2.5);
        grad.addColorStop(0, p.glow);
        grad.addColorStop(1, 'transparent');
        ctx.fillStyle = grad;
        ctx.beginPath();
        ctx.arc(px, py, p.radius * 2.5, 0, 6.28318);
        ctx.fill();

        // Planet Core
        ctx.fillStyle = p.color;
        ctx.beginPath();
        ctx.arc(px, py, p.radius, 0, 6.28318);
        ctx.fill();

        // Planet Orbital Rings
        if (p.ring) {{
          ctx.strokeStyle = p.color;
          ctx.lineWidth = 1.5;
          ctx.globalAlpha = 0.5;
          ctx.beginPath();
          ctx.ellipse(px, py, p.radius * 1.8, p.radius * 0.6, Math.PI / 6, 0, 6.28318);
          ctx.stroke();
          ctx.globalAlpha = 1.0;
        }}
      }});
    }}

    requestAnimationFrame(render);
  }}

  render();
}})();
"""


__all__ = ["SpaceCanvasConfig", "generate_space_canvas_script"]
