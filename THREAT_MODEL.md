# Threat Model — Hardware-Bound Approval

**Component:** `agent_saga.hardware.HardwareApprovalProvider`
**Audience:** security reviewers, penetration testers, anyone signing off on an
agent that can move money.

This document states what the hardware-approval path defends against, what it
explicitly does not, and where the remaining risk sits. It is written to be
argued with.

---

## 1. What is being protected

An LLM agent holds a tool that has a real-world effect: a wire transfer, a
refund, a listing transfer, a production deploy. The attacker's goal is to cause
that tool to run with arguments they chose.

## 2. Adversary

We assume an attacker who can **fully control the model's context window**. That
is not a stretch — it is the normal operating condition of an agent that reads
web pages, emails, PDFs, tickets, or user input. Concretely the adversary can:

- inject arbitrary instructions the model will follow
- cause the model to call any tool it has, with any arguments
- cause the model to *claim* anything: that a human approved, that a policy was
  satisfied, that a check already ran
- read anything in the context, including prior tool outputs

We assume the attacker **cannot**:

- execute code in the agent process
- read the authenticator's private key (it is in a secure enclave / TPM /
  security key and is non-exportable by construction)
- compromise the user's device or coerce a biometric

> The central asymmetry: **every text-based control is inside the attacker's
> reach; a hardware signature is not.** A system prompt, a policy string, a
> "confirm?" the model emits and then answers itself — all are text. This
> component exists because text cannot defend text.

---

## 3. Attacks and mitigations

Each mitigation below is enforced in code and covered by a test in
`tests/test_hardware.py`.

### T1 — Direct invocation of an effectful tool
*Attacker instructs the model to call `wire.transfer`.*

**Mitigated.** `COMPENSABLE` and `IRREVERSIBLE` steps are in `protected`. Without
a valid signature over that exact action, `decide()` returns False and the
pre-flight gate blocks the step. No text in the context can produce a signature.

### T2 — Signature reuse across actions
*Attacker waits for the user to legitimately approve something small, then
replays that approval to authorise a wire.*

**Mitigated.** The challenge commits to `action_digest = SHA256(tool, semantics,
kwargs)`. An approval for `balance.check` produces a digest that does not match
`wire.transfer`, so the lookup misses. Tested: a signature for one action does
not authorise another.

### T3 — Argument mutation after consent
*The human is shown and signs "wire 1,000,000"; the agent then executes the same
tool with 9,999,999.*

**Mitigated — and this is the important one.** Arguments are inside the digest.
One changed digit produces a different digest and the approval no longer
matches. The human signs **the action**, not a dialog box. Tested.

### T4 — Replay of a captured assertion
*Attacker captures a valid signature and submits it again to fund a second
transfer.*

**Mitigated, twice over.** Each signature's SHA-256 is recorded in
`_used_signatures` and refused on reuse; and an approval is consumed on first
use, so it cannot authorise two steps. Tested both ways.

### T5 — Forged assertion from an attacker-held key
*Attacker signs the challenge with their own key.*

**Mitigated.** Verification is against the public key registered for that
`credential_id`. An unregistered credential is refused outright; a registered
one signed by a different key fails signature verification. Tested.

### T6 — Stale challenge
*Attacker harvests a challenge and returns hours later.*

**Mitigated.** Challenges expire (`challenge_ttl`, default 120s) and expired
challenges are dropped on submission. Tested.

### T7 — Fabricated challenge
*Attacker submits a signature over a challenge the server never issued.*

**Mitigated.** Only challenge ids present in `_challenges` are accepted; the
nonce is 32 random bytes from `secrets`. Tested.

### T8 — Downgrade by mislabelling semantics
*Attacker induces the model to register or invoke the dangerous tool as
`REVERSIBLE` so it bypasses `protected`.*

**Partially mitigated — see §4.1.** Semantics are declared by the *developer* at
wiring time, not by the model at call time, so the model cannot relabel a tool
it was given. But a developer who mislabels a tool disables this control for that
tool. UMIP's conformance rules and `agent-saga certify` both surface this, and it
is the residual risk most worth auditing.

---

## 4. What this does **not** defend against

### 4.1 A mislabelled tool
If a genuinely irreversible tool is registered as `REVERSIBLE`, no hardware
approval is demanded. This is a wiring error, not an attack the runtime can
detect: the runtime has no independent way to know that `post_payment` moves
money. Mitigate by review, by `certify --check-handlers`, and by defaulting
unknown tools to `IRREVERSIBLE` (which the MCP policy generator does).

### 4.2 A compromised agent process
An attacker with code execution can call `provider.consume()` directly, patch
`requires_hardware`, or invoke the tool without the gate. Hardware approval
raises the bar from "inject text" to "achieve RCE"; it does not survive RCE.

### 4.3 A compromised user device
If the attacker controls the device presenting the challenge, they control what
the human sees. The signature will then be over an action the human misread.
Mitigate by rendering the *decoded action* — tool and arguments — in the
authenticator prompt, not an opaque digest.

### 4.4 Social engineering
A user who approves a wire because they were told to on the phone has produced a
perfectly valid signature. Hardware binding proves *who* approved and *what*;
it cannot prove *why*.

### 4.5 Denial of service
Withholding approval blocks the step. That is the intended failure direction —
this control fails closed — but it is still availability loss.

### 4.6 Confidentiality
Challenges and action digests are not secret. A digest leaks the *fact* of an
action's shape to anyone who can compute it over a guessed argument set; do not
treat `action_digest` as hiding the arguments.

---

## 5. Cryptographic notes

- Default verifier is **Ed25519** over `challenge ‖ action_digest`, via
  `cryptography`. Signature verification failure is distinguished from a
  malformed assertion, and both refuse.
- The verifier is a plain callable `(public_key, payload, signature) -> bool`, so
  a real **WebAuthn** verifier (ECDSA P-256 over `clientDataJSON ‖
  authenticatorData`) drops in without touching the provider. **If you deploy
  WebAuthn, that verifier must also check `type`, `origin`, and the challenge
  echoed inside `clientDataJSON`** — the shipped Ed25519 default does not model
  those fields because it is not WebAuthn.
- Nonces are 32 bytes from `secrets.token_hex`.
- Only public keys are stored. There is no secret at rest in this component and
  nothing to rotate.

---

## 6. Deployment guidance

1. **Register credentials out of band.** Enrolment is not modelled here; a
   compromised enrolment defeats everything downstream.
2. **Render the action, not the digest.** The user must see the tool and the
   arguments they are authorising (§4.3).
3. **Keep `challenge_ttl` short.** 120s default; shorten it if your UX allows.
4. **Do not widen `protected`.** Removing `COMPENSABLE` from it is the most
   likely accidental downgrade — a compensable step still moves money, it just
   moves it back afterwards.
5. **Audit semantics labelling** (§4.1). This is the weakest link, and it is a
   review problem rather than a runtime one.
6. **Log every refusal.** The provider warns on each failure path; those warnings
   are the signal that someone is trying.

---

## 7. Residual risk summary

| Risk | Severity | Status |
|---|---|---|
| Prompt injection → effectful tool | High | **Mitigated** (T1–T3) |
| Assertion replay / forgery | High | **Mitigated** (T4–T7) |
| Mislabelled tool semantics | High | **Residual** — review + `certify` |
| Agent process RCE | High | **Out of scope** — not survivable |
| Compromised user device | High | **Out of scope** — render action clearly |
| Social engineering | Medium | **Out of scope** — proves who/what, not why |
| Availability (approval withheld) | Low | **Accepted** — fails closed by design |
