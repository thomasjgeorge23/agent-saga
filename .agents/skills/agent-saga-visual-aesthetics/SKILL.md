---
name: agent-saga-visual-aesthetics
description: Bridges agent-saga backend transactional reliability (WAL, compensations, Pre-flight gates) with supreme frontend visual aesthetics, Framer Motion animations, glassmorphism, and dynamic micro-interactions.
---

# Agent-Saga Visual Aesthetics & Animation Bridge

This skill defines the architectural patterns for converting headless `agent-saga` backend transactional state events into stunning, world-class frontend animations, dynamic UI state transitions, and interactive visual feedback.

## Core Visual Mapping Architecture

```
┌──────────────────────────┐          SSE / REST          ┌──────────────────────────┐
│    agent-saga Engine     │ ───────────────────────────> │ Supreme Visual Frontend  │
│  (WAL, Gate, Recovery)   │   real-time event stream     │ (Framer Motion + Confetti)│
└──────────────────────────┘                              └──────────────────────────┘
             │                                                         │
             ▼                                                         ▼
    - SAGA_BEGIN                                              - Modal open + Ambient Mesh Glow
    - STEP_INTENT (tool)                                      - Node Pulse + Shimmer Trail
    - STEP_COMMIT (tool)                                      - Emerald Ring Lock + Checkmark
    - PREFLIGHT_BLOCK                                         - Amber Gate Alert + Micro-shake
    - ROLLBACK_START                                          - Red Wave Ripple + Reverse Motion
    - SAGA_FINISH (clean)                                     - Celebration Confetti Burst 🎉
```

## Aesthetic Principles for Saga-Driven UI

1. **Reactive State Synchrony**: Never show generic fake loading spinners. Map exact `agent-saga` step lifecycle states (`INTENT_LOGGED`, `COMMITTED`, `COMPENSATED`, `ABORTED`) to distinct kinetic visual animations.
2. **Glassmorphism & Ambient Illumination**: Use multi-layered dark backdrop filters (`backdrop-blur-md`), deep slate containers (`bg-slate-900/90`), and subtle radial ambient glows (`from-indigo-500/10 via-purple-500/10 to-emerald-500/10`).
3. **Micro-Interactions & Celebration Signals**:
   - Success triggers `canvas-confetti` particles and emerald glow rings.
   - Rollback triggers LIFO step retraction animation with amber warning badges.
   - Pre-flight block triggers a subtle alert card shake with policy violation details.
4. **WAL Event Stream Visual Ledger**: Provide an auditable, real-time visual timeline showing Write-Ahead Log sequence numbers, Fernet encryption status, and execution duration in milliseconds.

## Key Component Specifications

### 1. `SagaVisualLedger` (`src/components/SagaVisualLedger.tsx`)
A kinetic flow visualizer that subscribes to real-time saga events and renders:
- Step-by-step kinetic nodes with glowing borders.
- Live WAL terminal inspector with sequence numbers.
- Confetti particle triggers on 100% clean transaction commits.

### 2. `AgentSagaBadge` (`src/components/AgentSagaBadge.tsx`)
An interactive, micro-animated header badge featuring:
- Animated live pulse ring indicating active WAL durability.
- BYOK Fernet encryption status indicator.
- Hover scale spring transitions.
