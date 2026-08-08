# Contributing to `agent-saga` 🌌

Thank you for your interest in contributing to **`agent-saga`** — the world's leading transactional safety microkernel and rollback-proving framework for non-deterministic AI agent fleets.

`agent-saga` is published and maintained by **SAGAOPS Enterprise** under the leadership of Founder & Owner **Thomas J George** ([thomasjgeorge23@gmail.com](mailto:thomasjgeorge23@gmail.com)).

---

## 🏛️ Code of Conduct
We are committed to providing a welcoming, inclusive, and harassment-free environment for all contributors. Please review our [Code of Conduct](CODE_OF_CONDUCT.md) before participating.

## 🌟 Why Contribute to `agent-saga`?

`agent-saga` is rapidly becoming the standard safety primitive for autonomous AI agent systems (analogous to NumPy in data science or Temporal in microservices). Contributing to `agent-saga` positions you as a core pioneer in mission-critical AI infrastructure!

### 💡 High-Impact Ways to Contribute:
1. **Build a New Framework Adapter**: Connect `agent-saga` to your favorite framework (e.g. `instructor`, `dspy`, `guidance`, `semantic-kernel`).
2. **Add a New Service Connector**: Write compensator handlers for SaaS APIs (`twilio`, `sendgrid`, `aws-lambda`, `github-actions`).
3. **Submit a Good First Issue**: Look for issues tagged `good-first-issue` on GitHub.

---

## ⚡ Quickstart Development Setup

### 1. Clone the Repository
```bash
git clone https://github.com/thomasjgeorge23/agent-saga.git
cd agent-saga
```

### 2. Create Virtual Environment & Install Dependencies
```bash
python -m venv venv
# On Windows:
venv\Scripts\activate
# On Linux/macOS:
source venv/bin/activate

pip install -e ".[dev]"
```

### 3. Run the Test Suite
```bash
python -m pytest
```

---

## 🧪 Development Workflow & Guidelines

- **Zero Breaking Changes**: All PRs must pass the 60+ automated unit and integration tests without breaking backward compatibility.
- **Write-Ahead Log (WAL) Invariants**: Any modification to `agent_saga/wal/` or `agent_saga/kernel.py` must uphold the 7 transactional invariants verified by `verify_rollback_invariants()`.
- **Typing & Type Checking**: Code must pass `mypeline` / strict static type checking.

---

## 📬 Contact & Security Reporting
For security vulnerabilities, please email Founder **Thomas J George** directly at [thomasjgeorge23@gmail.com](mailto:thomasjgeorge23@gmail.com).

Thank you for helping make `agent-saga` as ubiquitous and foundational as NumPy! 🚀
