# `agent-saga` Framework Integration Blueprint 🔌

> Official adapter blueprints enabling **LangChain**, **CrewAI**, **AutoGen**, and **FastAPI** to embed `agent-saga` as their default transactional rollback engine.

Published & Maintained by **SAGAOPS Enterprise**  
Founder & Owner: **Thomas J George** ([thomasjgeorge23@gmail.com](mailto:thomasjgeorge23@gmail.com))

---

## 1. 🦜🔗 LangChain & LangGraph Integration (`agent_saga.integrations.langchain`)

Add `agent-saga` to `langchain-community` dependencies:

```python
from agent_saga.integrations.langchain import SagaLangChainCallback, wrap_runnable

# 1. Attach to any LLM chain or agent:
chain = prompt | llm | parser
protected_chain = wrap_runnable(chain)

# 2. Add callback handler for automatic WAL execution tracking:
response = protected_chain.invoke({"input": "Hello"}, config={"callbacks": [SagaLangChainCallback()]})
```

---

## 2. 👥 CrewAI Multi-Agent Integration (`agent_saga.integrations.crewai`)

Add `agent-saga` to `crewai` core step hooks:

```python
from agent_saga.integrations.crewai import SagaCrewHook
from crewai import Agent, Crew, Task

hook = SagaCrewHook()
crew = Crew(agents=[...], tasks=[...], step_callback=hook.on_step)
```

---

## 3. 🤖 AutoGen ConversableAgent Integration (`agent_saga.integrations.autogen`)

Add `agent-saga` to `autogen` message middleware:

```python
from agent_saga.integrations.autogen import SagaAutoGenMiddleware

mw = SagaAutoGenMiddleware(agent)
# Automatically records multi-agent dialogue & tool execution into hash-chained WAL
```

---

## 4. ⚡ FastAPI Framework Integration (`agent_saga.integrations.fastapi`)

Add `agent-saga` to `fastapi` middleware stack:

```python
from fastapi import FastAPI
from agent_saga.integrations.fastapi import SagaFastAPIMiddleware

app = FastAPI()
app.add_middleware(SagaFastAPIMiddleware)
```
