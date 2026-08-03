import pytest
from agent_saga.space_canvas import SpaceCanvasConfig, generate_space_canvas_script
from agent_saga.site_builder import AutonomousSiteBuilder


def test_space_canvas_generator():
    cfg = SpaceCanvasConfig(star_count=200, warp_speed=1.5)
    script = generate_space_canvas_script(cfg)
    assert "spaceCanvas" in script
    assert "200" in script
    assert "1.5" in script


def test_site_builder():
    def sample_tool(user: str, amount: float):
        return "ok"

    builder = AutonomousSiteBuilder(title="Cosmic Test App")
    builder.add_saga(sample_tool, title="Sample Tool Saga")
    html = builder.build_html()

    assert "Cosmic Test App" in html
    assert "spaceCanvas" in html
    assert "agent-saga" in html
