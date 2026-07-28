import pytest
from agent_saga.adapters import wrap_crew, wrap_langgraph, wrap_autogen, wrap_swarm, wrap_llamaindex


class DummyCrew:
    def kickoff(self, inputs):
        return f"kickoff_done_{inputs}"


class DummyLangGraph:
    def invoke(self, state):
        return {"state": "done"}


class DummyAutoGen:
    def generate_reply(self, messages):
        return "autogen_reply"


class DummySwarm:
    def run(self, agent, messages):
        return "swarm_response"


class DummyLlamaIndex:
    def chat(self, message):
        return "llamaindex_chat"


def test_framework_wrappers_execution():
    # 1. CrewAI
    crew = wrap_crew(DummyCrew())
    assert crew.kickoff("test") == "kickoff_done_test"

    # 2. LangGraph
    graph = wrap_langgraph(DummyLangGraph())
    assert graph.invoke({}) == {"state": "done"}

    # 3. AutoGen
    autogen = wrap_autogen(DummyAutoGen())
    assert autogen.generate_reply([]) == "autogen_reply"

    # 4. Swarm
    swarm = wrap_swarm(DummySwarm())
    assert swarm.run(None, []) == "swarm_response"

    # 5. LlamaIndex
    llama = wrap_llamaindex(DummyLlamaIndex())
    assert llama.chat("hello") == "llamaindex_chat"
