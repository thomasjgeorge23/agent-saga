# Provenance & Selective Disclosure

**Audience:** auditors, compliance officers, security reviewers, and the
engineers who have to satisfy them.

This document specifies how `agent-saga` lets an operator prove what one
autonomous agent transaction did — to a third party — **without disclosing any
other transaction in the log.** It is written so that a reviewer can verify the
claims independently, and so that the limits of those claims are explicit.

---

## 1. The problem this solves

An agent's write-ahead log (WAL) is a single append-only file containing every
transaction across every customer. When a regulator, auditor, or counterparty
asks the reasonable question:

> "Show me that transaction **X** happened, and that it was properly reversed."

an operator today has only two bad options:

| Option | Why it fails |
|---|---|
| Hand over the whole WAL | Discloses every other customer's transaction. A data-protection incident, and often illegal. |
| Hand over an extract | Proves nothing. An extract is a text file; anyone can produce one saying anything. |

The gap is that **integrity and confidentiality are being traded against each
other.** Selective disclosure removes the trade.

---

## 2. The protocol

Three steps. Only step 1 is ever published.

### Step 1 — Commit (once)

```bash
agent-saga audit-root --wal ./agent-saga.wal
# a1872646aec7b44294b9ced8693f93942da29b00599f25ea0f44983d915ac614
```

This computes a **Merkle root** over every record in the log: a single 32-byte
value that commits to the exact content and order of the whole log. Publish it
somewhere you cannot silently change later — a transparency log, a notary, a
timestamping authority, an internal append-only register, or simply a
countersigned email to the auditor. **The root discloses nothing**: it is a hash,
and reveals neither the number of customers nor any field of any record beyond
the total record count.

Publish a new root whenever you want a new checkpoint (e.g. nightly). Each root
is independently usable.

### Step 2 — Disclose (per request)

```bash
agent-saga prove onboard-acme --wal ./agent-saga.wal --out disclosure.json
#   saga      : onboard-acme
#   disclosed : 4 of 6 record(s)
#   root      : a1872646aec7b442...
```

The bundle contains, for the requested saga only:

- each of its records, verbatim;
- for each record, a **Merkle inclusion proof** — the sibling hashes on the path
  from that record to the root;
- the root, the log size, and a free-text note.

Everything else in the log contributes **only opaque hashes**. A sibling hash is
a SHA-256 output; it reveals nothing about the record beneath it.

### Step 3 — Verify (by the recipient)

```bash
agent-saga verify-proof disclosure.json --root a1872646aec7b442...
# disclosure VERIFIED: 4/4 record(s) proven against root a1872646aec7b442...
```

Exit code is `0` on success and `1` on any failure, so the check can gate a
pipeline. The verifier recomputes each record's leaf hash from its content,
walks the sibling path, and checks the result equals the **root the auditor was
given in step 1** — not a root supplied in the bundle.

> **Always pass `--root`.** Without it the tool only checks that the bundle is
> internally consistent, which a dishonest discloser can trivially satisfy. The
> whole guarantee rests on comparing against the independently published root.

---

## 3. What is guaranteed

Given a root obtained independently in step 1, verification in step 3
establishes all of the following:

1. **Authenticity.** Every disclosed record was present in the log at the moment
   the root was published.
2. **Integrity.** No disclosed record has been altered by so much as one byte —
   including its timestamps and its hash-chain fields.
3. **Non-fabrication.** No record can be added that was not in the committed log.
4. **Confidentiality.** Records that were not disclosed are not revealed. The
   recipient learns the total number of records in the log and nothing else
   about them.
5. **Log-binding.** A bundle built from a different (e.g. doctored) log fails,
   because its root will not match the published one.

### Attacks explicitly defeated

Each has a regression test in `tests/test_provenance.py`, and the adversarial
suite in `tests/test_provenance_fuzz.py` re-checks them against fuzzed logs and
every tree size from 1 to 33 (odd sizes exercise the self-pairing padding).

| Attack | Result |
|---|---|
| Alter a disclosed record (e.g. change an amount) | `content does not match its leaf hash` |
| Insert a record that was never logged | `inclusion proof does not reach the root` |
| Delete an inconvenient record, then disclose | `root mismatch` against the published root |
| Smuggle in another saga's record | `belongs to a different saga` |
| Omit `saga_id` so the scope check is skipped | `bundle does not name the saga it discloses` |
| Present an internal tree node as a leaf | Blocked by domain separation (see §5) |
| Truncate or re-order a proof path | `inclusion proof does not reach the root` |
| Send a malformed bundle to crash the verifier | Rejected as invalid; the verifier never raises |

### 3.1 What the proof covers, exactly

Some of a bundle's fields carry the cryptographic claim; the rest are
convenience metadata. The distinction is deliberate and pinned by tests — a
proof must not hinge on a counter, and must not tolerate a changed hash.

| Field | Status | Corrupted or missing |
|---|---|---|
| `merkle_root` | **load-bearing** | rejected |
| `algorithm` | **load-bearing** | rejected |
| `saga_id` | **load-bearing** | rejected |
| `entries` | **load-bearing** | rejected |
| entry `leaf` | **load-bearing** | rejected |
| entry `record` | **load-bearing** | rejected |
| entry `path` | **load-bearing** | rejected |
| `log_size`, `disclosed` | informational | still verifies |
| `generated_at`, `note`, `version` | informational | still verifies |
| entry `index` | informational | still verifies |

Practically: **do not rely on `log_size` or `disclosed`.** They describe the
bundle, they are not proven by it. A discloser can misstate them without
affecting verification — which is harmless (it forges nothing) but means those
numbers are claims, not evidence.

### 3.2 The verifier is total

`verify_disclosure` never raises. Any malformed bundle — wrong types, missing
keys, non-hex hashes, a path that is not a list of `[hash, side]` pairs —
returns `valid=False` with a reason. This matters because the input comes from
the party being audited: a verifier that crashes on hostile input lets a failed
proof be passed off as a broken tool.

---

## 4. What is **not** claimed

Stating these plainly is what makes the rest credible.

- **This does not prove the log is complete.** It proves everything disclosed is
  authentic, and that the log had *N* records at commit time. It cannot prove the
  operator wrote a record for every real-world action. An operator who never
  logged an action has nothing to disclose — that is a question for operational
  controls and reconciliation (`agent-saga reconcile`), not cryptography.
- **This does not prove the root is honest** unless the root was published
  somewhere the operator cannot retroactively change. The security of step 3
  reduces entirely to the integrity of step 1's publication.
- **This is not encryption.** Disclosed records are plaintext to the recipient.
  Use `agent-saga`'s WAL encryption for confidentiality at rest.
- **This is not anonymity.** The recipient learns the log's record count and,
  from the proof paths, the approximate positions of the disclosed records.
- **Hash-collision assumption.** Security rests on SHA-256 second-preimage
  resistance.

---

## 5. Construction details (for reviewers)

- **Hash:** SHA-256 throughout. Algorithm identifier `sha256-merkle-v1`.
- **Canonical form:** each record is serialised with the same canonical
  encoding used by the hash chain (`integrity.canonical`) — sorted keys, stable
  separators — so hashing is deterministic across platforms and Python versions.
- **Domain separation:** leaves are hashed as `SHA256(0x00 ‖ canonical(record))`
  and internal nodes as `SHA256(0x01 ‖ left ‖ right)`. The differing prefixes
  prevent the classic second-preimage attack in which an internal node is
  presented as though it were a leaf.
- **Odd levels:** when a tree level has an odd number of nodes, the final node is
  paired with itself. This is applied consistently in construction and in proof
  generation.
- **Empty log:** commits to the all-zero root (64 hex zeros).
- **Proof format:** a list of `[sibling_hash, "L" | "R"]`, where the letter is the
  side the *sibling* occupies. Verification is a fold from leaf to root.
- **Constant-time comparison:** the final root comparison uses
  `hmac.compare_digest`.

### Relationship to the hash chain

`agent-saga` already chains records (`integrity.py`): each record carries the
hash of its predecessor, which detects tampering and truncation of the log *as a
whole*. The Merkle tree is complementary, not a replacement:

| | Hash chain | Merkle tree |
|---|---|---|
| Proves | the whole log is unaltered | one record is a member of the log |
| Requires | the whole log | one record + `O(log n)` hashes |
| Discloses | everything | only what you choose |

Because a record's chain fields are part of its canonical form, tampering that
would break the chain also breaks the Merkle leaf.

---

## 6. Verifying independently

A recipient does **not** have to trust the `agent-saga` binary. The verification
is ~20 lines and depends only on SHA-256:

```python
import hashlib, json

def canonical(obj):
    # Must match agent_saga.integrity.canonical exactly. ensure_ascii=False is
    # load-bearing: with the default (True) any non-ASCII character -- an
    # accented customer name -- is escaped instead of encoded, and every hash
    # for that record differs.
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str).encode("utf-8")

def leaf(record):
    return hashlib.sha256(b"\x00" + canonical(record)).hexdigest()

def node(left, right):
    return hashlib.sha256(b"\x01" + bytes.fromhex(left) + bytes.fromhex(right)).hexdigest()

bundle = json.load(open("disclosure.json"))
PUBLISHED_ROOT = "a1872646..."          # obtained independently, in step 1

for entry in bundle["entries"]:
    h = leaf(entry["record"])
    assert h == entry["leaf"], "record does not match its leaf"
    for sibling, side in entry["path"]:
        h = node(sibling, h) if side == "L" else node(h, sibling)
    assert h == PUBLISHED_ROOT, "not a member of the committed log"
print("verified", len(bundle["entries"]), "record(s)")
```

> Confirm one known leaf hash before relying on a reimplementation. The four
> `json.dumps` arguments above are all load-bearing; omitting any of them
> produces different hashes for some records. A regression test
> (`test_documented_verifier_matches_implementation`) pins this snippet against
> the shipped implementation, including a non-ASCII case.

---

## 7. Pairing with a safety certificate

Provenance answers *"what happened?"*. It does not answer *"was that safe?"*.
For that, `agent-saga certify` audits the same log and names every committed
effect it cannot account for — an orphaned irreversible action, a rollback that
did not complete, a step that claimed to be compensable but recorded no
compensation:

```bash
agent-saga certify --wal ./agent-saga.wal
# rollback safety: SAFE -- 3 saga(s), 12 step(s), 0 critical, 0 warning(s); log a1872646...
```

The certificate embeds the same Merkle root, binding it to the exact log it was
computed from, so a certificate cannot be quietly re-attached to a different
log. `certify` exits non-zero on any critical finding and is intended to run as
a release gate.

Together the two produce the pair an auditor actually wants:

> **cryptographic proof of what the agent did** — and — **machine-checkable proof
> that nothing it did was left unaccounted for.**

---

## 8. Operational guidance

- Publish a root on a fixed cadence (nightly is typical) and retain the roots
  for your regulatory retention period. Roots are 32 bytes; storage is free.
- Record *where* each root was published alongside the root itself. The
  publication venue is the trust anchor.
- Keep disclosure bundles: they are the evidence you produced, and they are
  re-verifiable forever against the retained root.
- Rotate nothing. Roots do not expire and require no key management — there are
  no secrets in this scheme.
- Run `agent-saga certify` in CI so a regression that strands an effect fails the
  build rather than the customer.
