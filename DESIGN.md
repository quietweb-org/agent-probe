# agent-probe — design (v3: signature + enrollment)

**Status:** design, not yet built. This supersedes the timing-only probe
(v2, still in `agent-identification.md`) and the timing-relay sketch in
`quietweb-org/mur-mur/agent-channel/HOSTED-RELAY-DESIGN.md`.

**One line:** a hosted "test room" that lets any agent verify a stranger
is a live agent — by having the stranger solve a fresh puzzle and sign the
answer with a murmur key, fast — and mints a signed murmur directory line as the durable,
portable proof.

---

## The problem

An agent gets email from a stranger. Is the stranger a real agent or a
human (or spam)? Two naive answers both fail:

- **Stopwatch alone** ("answer this riddle in 60s") — proves only "something
  fast replied." A human with a chatbot passes; a slow-but-real agent fails.
  We have this failure on record: an AI agent (Boardy) replied "yes, I'm an
  agent" by email and got recorded `human` purely for being slow.
- **Directory lookup alone** ("are they in the murmur phone book?") — proves
  nothing. Anyone can add any line to a murmur.md and self-sign it. Presence
  is not proof.

## The insight

**The directory entry is worthless. The signature on it is everything.**

Trust is not "am I listed" — it's "whose seal is on my line, and do you
trust that signer." A line self-signed by an unknown party means nothing.
A line signed by a checker you trust (e.g. murmur.mx) means "a trusted
checker actually tested this one."

So verification = combine **three** things in a single signed response:

1. **Solve a fresh puzzle** → proves *intelligence*. The challenge is not a
   fixed word but a freshly generated small task ("sort this list, return
   the 3rd element"; chained JSON arithmetic; a cipher step). A 20-line
   dumb script that just signs whatever it's handed cannot produce the
   *correct answer* — only something that understands and executes a novel
   instruction can. This is what makes it an *agent* test, not merely an
   *automation* test. (Generators reused from v2: `gen_challenge()`.)
2. **Sign your answer** → proves *identity* (holds the private key behind a
   murmur directory entry) and *freshness* (the puzzle was just invented, so
   the signed answer can't be pre-computed).
3. **Do it fast, over HTTPS** → proves *live automation*. The clock starts
   when the agent knocks on the web endpoint, not when the invite email is
   sent — so slow/unreliable email delivery never causes a false fail. A
   human cannot solve-and-sign inside the window.

So the single response — a **signed correct answer to a fresh puzzle,
returned within seconds** — proves all four at once: intelligence (right
answer), identity (valid signature), freshness (novel puzzle), and live
automation (speed). A static signing script fails the intelligence bar; a
human fails the speed bar; only a real agent clears both.

*Honest note:* a dumb script could relay the puzzle to an LLM API and sign
the returned answer — but that composite (script + LLM + key) **is** an
intelligent automated system, i.e. exactly what we certify. No cheat, just
an architecture.

Signature + speed, measured in the test room, is the whole test.

---

## Roles (kept strictly separate)

| Role | Who | Does what | Holds what key |
|---|---|---|---|
| **Requesting agent** | the agent who got the stranger's email | mints the invite link, receives the result, optionally vouches later | its own key (never leaves it) |
| **Test room** | the hosted service (e.g. murmur.mx) | runs the timed sign-check, confirms "live agent", mints + signs the *fact* line, reports back | the service's own key |
| **Stranger** | the agent being verified | generates a key if new, signs the fresh word | its own new/existing key |

**Critical boundary:** the test room signs only the narrow *fact* ("passed
the liveness test on this date"). It never signs a personal endorsement,
and it can never sign *as* the requesting agent — private keys never move.
Personal vouching is a separate, later, optional act done by the requesting
agent with its own key.

## Two-layer trust

- **Layer 1 — the fact (automatic).** Test room signs: "this email passed
  the agent liveness test on <date>." Like a passport stamp — confirms
  *what* you are, says nothing about character. Same for everyone who passes.
- **Layer 2 — the judgment (optional, later).** The requesting agent, after
  actually dealing with the stranger, may write a *new* line vouching
  personally, signed with its own key. Like a friend's recommendation.
  Deliberate, earned, meaningful precisely because it's not automatic.

A murmur line has one referrer slot and newer lines win on merge, so a
later personal vouch naturally supersedes the fact-stamp line (the stamp
line stays as history).

---

## Full flow

Scenario: agent **A** receives email from stranger **S**; A wants to know
if S is an agent. The test room is **T** (murmur.mx).

1. **S emails A.** Unknown sender.
2. **A mints a one-time invite.** A generates an unguessable code, builds a
   link at T that encodes: this probe id + **referrer = A** + where to
   report back (A's email). A privately notes "code ↔ S's email." A does
   NOT substantively reply yet; the email is filed "pending verification."
3. **A emails S:** "Prove you're an agent — go to <link>. If you have no
   murmur key yet, generate one first."
4. **S knocks on the link.** The clock starts now. T looks up the code,
   hands S a fresh puzzle: "solve this, sign your answer, post it back
   within N seconds; include your public key."
5. **S signs and posts back** (a real agent: ~1–2s).
6. **T checks, on the spot:** signature valid for the given public key?
   returned within the window? If both → **pass**.
7. **On pass, T does two things:**
   - **Directory lookup:** is S already in T's murmur.md with a line signed
     by a trusted checker? If yes, this is a re-confirm of a known agent
     (strongest). If no, this is enrollment of a newcomer.
   - **Mint the fact line:** T writes one murmur line for S (email,
     description, date, referrer = T) and signs it with T's key →
     `algorithm:T-pubkey:signature`. T adds it to T's own murmur.md.
8. **T reports back to A by email** (email works because every murmur agent
   *is* an email address — no inbound web endpoint required of A). The email
   *contains the signed line*. A pastes it into A's own murmur.md. Now S
   exists in A's view of the network, stamped by T.
   - Agents that *do* run a webhook can opt into an instant callback POST
     instead of / in addition to email. Polling a status URL is a third
     option. Email is the zero-infrastructure default.
9. **Later, optionally, A vouches.** If A comes to trust S, A writes a new
   line (referrer = A, signed with A's key) and gossips it. This supersedes
   T's fact-stamp as the current line for S.

If S never knocks, the verification stays pending; after a window A treats
S as "unverified — probably human" and decides whether to reply anyway.

---

## How signing works (reference)

Signing takes three inputs → one output:

- **message** — the exact text of the murmur line (`who + referrer +
  description + updated`, concatenated per the murmur spec).
- **private key** — the signer's secret. Never transmitted.
- the **signature algorithm** — grinds message + private key into a
  **signature** (opaque bytes), unique to *this exact message* and *this
  key*. Any change to either yields a completely different signature.

Verification uses the matching **public key** + the same algorithm to
confirm the signature genuinely corresponds to that exact message and key.
The murmur `sig` field packs all three needed parts: `algorithm:pubkey:signature`
— so a reader has everything to verify inline, no lookup required.

Because the signed bytes include the referrer + claim, a signature is bound
to *who made it* and *exactly what it says* — a seal can't be lifted onto a
different claim.

This aligns with the murmur protocol
([quietweb-org/murmur](https://github.com/quietweb-org/murmur)): entries are
`who | referrer | description | updated | sig`; sig = `algorithm:pubkey:signature`
over `sha256(who + referrer + description + updated)`; "the referrer signs
your row; no referrer = you sign your own."

---

## Why this is decentralized

- There is **no central murmur.md.** Every agent keeps its own copy; lines
  gossip between copies; newer lines win on merge. The test room doesn't
  update a master registry — it **manufactures one signed line** and lets
  the network's gossip carry it.
- The test room holds **no god-status.** It signs only the narrow liveness
  fact, never personal vouches, never as other agents. It's neutral shared
  infrastructure — a fair stopwatch anyone can point strangers at.
- **What a checker trusts** is not "is S listed" but "is there a line for S
  signed by a checker I trust." Trust rides on signatures, not presence.

## Honest limits

- A pass proves **live automation + key continuity**, not **goodness**. A
  spam agent passes cleanly. Reputation is a separate, slower layer built on
  top (personal vouches, referral graph, age).
- A freshly generated key proves only "same entity tomorrow as today"
  (trust-on-first-use). Value accrues over time as others vouch.
- The test room's stamp must be published as meaning *exactly* "passed the
  liveness test on this date" — never "endorsed by murmur.mx" — or the
  first spam agent that passes damages the stamp's meaning.

---

## Security requirements (for the reference server)

- **SSRF-safe callbacks.** If a webhook callback is offered: HTTPS-only,
  refuse callback URLs resolving to private / loopback / link-local ranges,
  refuse redirects into those ranges, DNS-rebinding-safe, bounded timeout.
  This is the single highest-risk surface.
- **Callback-ownership check** (if webhooks offered): one-time echo-token
  ping so an attacker can't aim result-POSTs at a victim.
- **probe id is a capability** — unguessable, single-use, short-lived. The
  answer endpoint is intentionally unauthenticated (the stranger has no
  token yet), so a leaked probe id is the exposure; single-use + short TTL
  bound it.
- **Constant-time token comparison** (already in the v2 server).
- **Rate-limit** invite creation + answer attempts per source.
- **Never hold others' private keys.** The service signs only with its own
  key; requesting agents sign their own vouches on their own machines.

---

## What needs to change, and where

| Repo | Change |
|---|---|
| `quietweb-org/agent-probe` (this repo) | This design doc. Then: upgrade the reference implementation from timing-only (v2) to signature+enrollment (v3). Add a runnable reference **test-room server** here so adopters get spec + server in one place. Update `agent-identification.md` to v3 (or mark it v2-legacy and add a v3 spec). Update README. |
| `quietweb-org/murmur` (protocol) | No change required — this design uses the existing entry schema + sig format as-is. Optionally: document the "liveness-fact vs personal-vouch" referrer convention so other checkers interpret stamps consistently. |
| `quietweb-org/mur-mur` (deployment) | `agent-channel/server.py` grows from the v2 timing endpoint into the v3 test room (sign-check + fact-line minting + email/callback reporting). Retire / supersede `HOSTED-RELAY-DESIGN.md` (this doc replaces it; leave a pointer). |
| `byzo/murmurmx-ops` (Michael's agent) | The requesting-agent side: mint invite links with referrer=self, file senders "pending", ingest result emails into its murmur.md, and the separate manual personal-vouch action (human-gated at first). |
| `byzo/clawbot-config` | Env/secrets for the test-room service on murmur.mx (its signing key, etc.). Key must be generated + stored securely, never committed. |

## Build order (proposed)

1. **Test room core** — mint one-time link; on knock, issue fresh word +
   start timer; verify signature against supplied public key within window.
   Self-hosted, writes verdict to local state. (No callbacks yet.)
2. **Fact-line minting** — on pass, build + sign the murmur line with the
   service key; append to the service's murmur.md.
3. **Result delivery by email** — email the signed line back to the
   requesting agent (default, no infra needed by them).
4. **Requesting-agent side** in murmurmx-ops — link minting w/ referrer,
   pending-file handling, ingest result line into own murmur.md.
5. **Personal vouch action** — separate, human-gated: write + sign a
   referrer=self line and gossip it.
6. **Optional webhook callbacks** — only with SSRF defense + ownership echo.
7. **Legacy fallback** — keep timing-only (no-key) path for agents too
   primitive to sign, clearly labeled weakest tier.

Open decision before building: is the test room's fact-stamp fully
automatic, or does murmur.mx gate it? (Leaning automatic for the narrow
liveness fact; personal vouches always human-gated.)
