name: ✨ Feature Request
description: Suggest an idea or enhancement for agent-saga
title: "[FEATURE]: "
labels: ["enhancement"]
assignees: ["thomasjgeorge23"]
body:
  - type: markdown
    attributes:
      value: |
        Have an idea to make `agent-saga` even more powerful and ubiquitous? Propose it here!
  - type: textarea
    id: feature-description
    attributes:
      label: Proposed Feature
      description: Describe the feature, use-case, and why it benefits the AI agent ecosystem.
    validations:
      required: true
  - type: textarea
    id: code-example
    attributes:
      label: Example API Usage
      description: Show how the proposed API might look in Python (`import agent_saga as saga`).
      render: python
