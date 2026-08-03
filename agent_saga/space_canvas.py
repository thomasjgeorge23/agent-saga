"""`agent_saga/space_canvas.py` -- High-Level Cosmic Space Canvas & Shader Generator for agent-saga.

Generates self-contained HTML5 Canvas & WebGL 3D cosmic space animation engines featuring
starfield warp physics, orbital saga transaction nodes, and cursor gravity lens distortion.

Published & Maintained by: SAGAOPS Enterprise
Founder & Owner: Thomas J George (thomasjgeorge23@gmail.com)
"""

from __future__ import annotations

from typing import Dict, List, Optional


class SpaceCanvasConfig:
    def __init__(
        self,
        star_count: int = 300,
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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "star_count": self.star_count,
            "enable_gravity_well": self.enable_gravity_well,
            "nebula_color_1": self.nebula_color_1,
            "nebula_color_2": self.nebula_color_2,
            "nebula_color_3": self.nebula_color_3,
            "warp_speed": self.warp_speed,
        }


def generate_space_canvas_script(config: Optional[SpaceCanvasConfig] = None) -> str:
    """Generate self-contained JavaScript space animation engine for HTML insertion."""
    cfg = config or SpaceCanvasConfig()

    return f"""
/* agent-saga v0.6.2 High-Level Cosmic Space Animation Engine */
(function() {{
  const canvas = document.getElementById('spaceCanvas') || (function() {{
    const c = document.createElement('canvas');
    c.id = 'spaceCanvas';
    c.style.cssText = 'position:fixed;top:0;left:0;width:100vw;height:100vh;pointer-events:none;z-index:0;';
    document.body.prepend(c);
    return c;
  }})();

  const ctx = canvas.getContext('2d');
  if (!ctx) return;

  let width = canvas.width = window.innerWidth;
  let height = canvas.height = window.innerHeight;

  window.addEventListener('resize', () => {{
    width = canvas.width = window.innerWidth;
    height = canvas.height = window.innerHeight;
  }});

  const mouse = {{ x: width / 2, y: height / 2, targetX: width / 2, targetY: height / 2 }};
  window.addEventListener('mousemove', (e) => {{
    mouse.targetX = e.clientX;
    mouse.targetY = e.clientY;
  }});

  const stars = Array.from({{ length: {cfg.star_count} }}, () => ({{
    x: (Math.random() - 0.5) * width * 2,
    y: (Math.random() - 0.5) * height * 2,
    z: Math.random() * width,
    radius: Math.random() * 1.8 + 0.5,
    color: ['{cfg.nebula_color_1}', '{cfg.nebula_color_2}', '{cfg.nebula_color_3}', '#ffffff'][Math.floor(Math.random() * 4)]
  }}));

  function render() {{
    mouse.x += (mouse.targetX - mouse.x) * 0.05;
    mouse.y += (mouse.targetY - mouse.y) * 0.05;

    ctx.fillStyle = 'rgba(5, 8, 17, 0.25)';
    ctx.fillRect(0, 0, width, height);

    const cx = width / 2;
    const cy = height / 2;

    stars.forEach((s) => {{
      s.z -= {cfg.warp_speed} * 1.5;
      if (s.z <= 0) s.z = width;

      const k = 256 / s.z;
      const px = s.x * k + cx;
      const py = s.y * k + cy;

      if (px >= 0 && px <= width && py >= 0 && py <= height) {{
        const size = (1 - s.z / width) * s.radius * 2.5;
        const opacity = (1 - s.z / width);

        // Gravitational lens distortion near cursor
        let dx = mouse.x - px;
        let dy = mouse.y - py;
        let dist = Math.sqrt(dx * dx + dy * dy);
        let pullX = 0, pullY = 0;
        if (dist < 200 && {str(cfg.enable_gravity_well).lower()}) {{
          pullX = (dx / dist) * (200 - dist) * 0.15;
          pullY = (dy / dist) * (200 - dist) * 0.15;
        }}

        ctx.fillStyle = s.color;
        ctx.globalAlpha = opacity;
        ctx.beginPath();
        ctx.arc(px + pullX, py + pullY, size, 0, Math.PI * 2);
        ctx.fill();
        ctx.globalAlpha = 1.0;
      }}
    }});

    requestAnimationFrame(render);
  }}

  render();
}})();
"""


__all__ = ["SpaceCanvasConfig", "generate_space_canvas_script"]
