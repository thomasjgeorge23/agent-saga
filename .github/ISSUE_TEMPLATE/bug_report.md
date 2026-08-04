name: 🐛 Bug Report
description: Create a report to help us improve agent-saga
title: "[BUG]: "
labels: ["bug"]
assignees: ["thomasjgeorge23"]
body:
  - type: markdown
    attributes:
      value: |
        Thank you for reporting a bug! Please fill out the details below so Founder Thomas J George and the SAGAOPS team can investigate and fix it promptly.
  - type: textarea
    id: description
    attributes:
      label: Bug Description
      description: A clear and concise description of what the bug is.
    validations:
      required: true
  - type: textarea
    id: traceback
    attributes:
      label: Traceback / Code Snippet
      description: Paste the exact log traceback or python code snippet.
      render: python
  - type: input
    id: environment
    attributes:
      label: Environment Info
      description: Python version, OS (Windows/Linux/macOS), agent-saga version (`python -c "import agent_saga; print(agent_saga.__version__)"`).
