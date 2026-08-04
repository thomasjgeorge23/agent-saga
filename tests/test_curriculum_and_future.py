"""`tests/test_curriculum_and_future.py` -- Unit tests for CS educational curriculum and futuristic agent engine.
"""

import pytest
import agent_saga as saga


def test_agent_curriculum_lessons():
    lessons = saga.learn()
    assert len(lessons) == 3
    assert "Lesson 1" in lessons[0]["title"]
    assert "Lesson 2" in lessons[1]["title"]
    assert "Lesson 3" in lessons[2]["title"]


@pytest.mark.anyio
async def test_futuristic_agent_execution():
    agent = saga.create_future_agent("TestFutureAgent")
    res = await agent.execute_synthesized_goal("Build planetary agent swarm")
    assert res["agent"] == "TestFutureAgent"
    assert res["status"] == "EXECUTED_WITH_QUANTUM_WAL_SAFETY"
