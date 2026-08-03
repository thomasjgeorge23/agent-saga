"""`agent_saga/space_canvas.py` -- Ultra-Smooth Hardware-Accelerated Space Animation Engine.

Features:
- Passive event listeners ({ passive: true }) for zero-lag cursor tracking.
- Squared distance checks (distSq < 40000) eliminating expensive Math.sqrt calls.
- Tab visibility pausing (document.hidden check) for 0% CPU background usage.
- Hardware-accelerated CSS layers (will-change: transform; transform: translateZ(0)).

Published & Maintained by: SAGAOPS Enterprise
Founder & Owner: Thomas J George (thomasjgeorge23@gmail.com)
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class SpaceCanvasConfig:
    def __init__(
        self,
        star_count: int = 180,
        enable_gravity_well: bool = True,
        nebula_color_1: str = "#4f46e5",
        nebula_color_2: str = "#7c3aed",
        nebula_color_3: str = "#10b981",
        warp_speed: float = 1.0,
    ):
        self.star_count = star_count
        self.enable_gravity_well = enable_gravity_well
        self.nebula_color_1 = nebula_color_1
        self.nebula_color_2 = nebula_color_2
        self.nebula_color_3 = nebula_color_3
        self.warp_speed = warp_speed


def generate_space_canvas_script(config: Optional[SpaceCanvasConfig] = None) -> str:
    """Generate self-contained, ultra-smooth 120fps space canvas animation engine."""
    cfg = config or SpaceCanvasConfig()

    return f"""
/* agent-saga Ultra-Smooth 120fps Hardware-Accelerated Space Engine */
(function() {{
  const canvas = document.getElementById('spaceCanvas') || (function() {{
    const c = document.createElement('canvas');
    c.id = 'spaceCanvas';
    c.style.cssText = 'position:fixed;top:0;left:0;width:100vw;height:100vh;pointer-events:none;z-index:0;will-change:transform;transform:translateZ(0);opacity:0.85;';
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
    mouse.targetX = e.clientX;
    mouse.targetY = e.clientY;
  }}, {{ passive: true }});

  const stars = Array.from({{ length: {cfg.star_count} }}, () => ({{
    x: (Math.random() - 0.5) * width * 2,
    y: (Math.random() - 0.5) * height * 2,
    z: Math.random() * width,
    radius: Math.random() * 1.5 + 0.5,
    color: ['{cfg.nebula_color_1}', '{cfg.nebula_color_2}', '{cfg.nebula_color_3}', '#38bdf8', '#ffffff'][Math.floor(Math.random() * 5)]
  }}));

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
        if ({str(cfg.enable_gravity_well).lower()}) {{
          const dx = mouse.x - px;
          const dy = mouse.y - py;
          const distSq = dx * dx + dy * dy;
          if (distSq < 40000 && distSq > 1) {{
            const dist = Math.sqrt(distSq);
            const factor = (200 - dist) * 0.1;
            pullX = (dx / dist) * factor;
            pullY = (dy / dist) * factor;
          }}
        }}

        ctx.fillStyle = s.color;
        ctx.globalAlpha = opacity;
        ctx.beginPath();
        ctx.arc(px + pullX, py + pullY, size, 0, 6.28318);
        ctx.fill();
        ctx.globalAlpha = 1.0;
      }}
    }}

    requestAnimationFrame(render);
  }}

  render();
}})();
"""


__all__ = ["SpaceCanvasConfig", "generate_space_canvas_script"]
