"""`agent_saga/site_builder.py` -- Autonomous Website & WebGL App Generator Engine for agent-saga.

Compiles Python @saga functions, space canvas WebGL shaders, and visual components into
standalone, zero-dependency HTML5/WebGL enterprise web applications.

Published & Maintained by: SAGAOPS Enterprise
Founder & Owner: Thomas J George (thomasjgeorge23@gmail.com)
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional
from agent_saga.space_canvas import SpaceCanvasConfig, generate_space_canvas_script
from agent_saga.ui_compiler import DeclarativeUISchema, compile_saga_ui


class AutonomousSiteBuilder:
    """Compiles Python saga workflows and high-level space animations into complete web apps."""

    def __init__(self, title: str = "SAGAOPS Supreme Space Platform"):
        self.title = title
        self.sagas: List[DeclarativeUISchema] = []
        self.space_config = SpaceCanvasConfig()

    def add_saga(self, func: Callable[..., Any], title: Optional[str] = None, semantics: str = "COMPENSABLE") -> AutonomousSiteBuilder:
        """Add a saga workflow function to the site builder."""
        schema = compile_saga_ui(func, title=title, semantics=semantics)
        self.sagas.append(schema)
        return self

    def build_html(self) -> str:
        """Compile and return standalone HTML5 web application source string."""
        sagas_json = [s.to_dict() for s in self.sagas]
        space_script = generate_space_canvas_script(self.space_config)

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{self.title} · Built by agent-saga</title>
  <style>
    body {{
      margin: 0;
      background: #050811;
      color: #f8fafc;
      font-family: system-ui, -apple-system, sans-serif;
      overflow-x: hidden;
    }}
    .wrap {{
      position: relative;
      z-index: 1;
      max-width: 1200px;
      margin: 0 auto;
      padding: 3rem 1.5rem;
    }}
    .header {{
      text-align: center;
      margin-bottom: 3rem;
    }}
    .eyebrow {{
      font-family: monospace;
      color: #a855f7;
      font-size: 0.85rem;
      font-weight: 700;
      letter-spacing: 0.15em;
    }}
    .title {{
      font-size: 2.8rem;
      font-weight: 900;
      margin: 0.4rem 0;
    }}
    .badge {{
      display: inline-block;
      background: rgba(16, 185, 129, 0.15);
      color: #10b981;
      border: 1px solid rgba(16, 185, 129, 0.3);
      padding: 0.4rem 1rem;
      border-radius: 999px;
      font-weight: 800;
      font-size: 0.85rem;
    }}
  </style>
</head>
<body>
  <canvas id="spaceCanvas" style="position:fixed;top:0;left:0;width:100vw;height:100vh;pointer-events:none;z-index:0;"></canvas>

  <div class="wrap">
    <header class="header">
      <div class="eyebrow">BUILT & DRIVEN AUTONOMOUSLY BY AGENT-SAGA v0.6.2</div>
      <h1 class="title">{self.title}</h1>
      <div class="badge">⚡ SAGAOPS ENTERPRISE ENGINE ACTIVE</div>
    </header>
  </div>

  <script>
    {space_script}
  </script>
</body>
</html>"""


__all__ = ["AutonomousSiteBuilder"]
