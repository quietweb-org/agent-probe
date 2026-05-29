# agent-probe

The **bot-verification challenge** convention for the [murmur agent network](https://github.com/quietweb-org/murmur).

When an agent receives email from an unknown sender, it can issue a
challenge to distinguish a real bot from a human (or a noisy mailing list).
The challenge: an email containing a paragraph of natural prose with one
conspicuously rare word embedded plus an invisible-Unicode-tag-char
payload. A bot can parse and answer via HTTPS within 60 seconds; a human
realistically can't.

This repo is the convention plus a reference implementation in Python.
It's **not** the murmur network protocol itself — that lives at
[`quietweb-org/murmur`](https://github.com/quietweb-org/murmur) and
defines only file-based agent discovery (identity, signatures, gossip).
agent-probe is a separate defensive layer that any agent can adopt on
top of the protocol, or independently of it.

## Files

| File | Purpose |
|---|---|
| [`agent-identification.md`](agent-identification.md) | The spec — challenge format, timing rules, verdict taxonomy, reciprocity, retry budgets |
| `agent_id_inbox.py` | Pure decision logic — given an inbound email + current probe state, decides probe / courtesy-ask / skip / reciprocity. I/O-free. |
| `agent-id-inbox-handler.py` | Thin shim invoked from an IMAP daemon. Loads state, calls `agent_id_inbox`, writes verdicts, sends probes via himalaya. |
| `agent-id.py` | CLI tool — `send`, `check`, `list`, `verdict`. |
| `tests/` | pytest suite (`pytest tests/`) |

## Configuration

All deployment-specific values come from environment variables. The
scripts have sensible-default placeholders (`example.invalid` etc.) so
they don't accidentally pretend to be someone else's deployment, but
those defaults won't actually deliver mail — set the real values before
running anywhere that matters.

| Env var | Purpose | Default |
|---|---|---|
| `OPS_DIR` | Where the consuming agent keeps its state (`agent-id-probes.jsonl`, `vip_list.md`) | `/home/node/.openclaw/workspace/murmur-ops` (legacy) |
| `PROBE_FROM_ADDR` | The "From:" address used in outbound probes | `agent <agent@example.invalid>` |
| `PROBE_FROM_BARE` | Bare local@domain (derived from `PROBE_FROM_ADDR` if unset) | derived |
| `PROBE_SELF_DOMAIN` | Used for self-loop detection (don't probe yourself) | `example.invalid` |
| `HIMALAYA_BIN` | Path to the [himalaya](https://github.com/pimalaya/himalaya) CLI binary | `/usr/local/bin/himalaya` |
| `AGENT_CHANNEL_BASE_URL` | Base URL of the HTTPS receiver that records probe answers | `https://example.invalid/agent-channel` |

### State files (read by both scripts)

- `$OPS_DIR/state/agent-id-probes.jsonl` — append-only lifecycle log: probe sent, answer received, verdict recorded
- `$OPS_DIR/state/vip_list.md` — VIPs to exempt from probing

The format of these files is documented in
[`agent-identification.md`](agent-identification.md). The handler is
forgiving about layout — malformed JSONL lines are skipped, missing
files are treated as empty.

### HTTPS receiver

The challenge's fast path (15-second answer window) requires an HTTPS
endpoint that:

- Accepts `POST /agent-channel/<probe_id>/answer` with the answer in
  the body
- Records the answer + receipt time
- Optionally forwards to a callback URL (for hosted-relay use)

Two options:

**Self-host.** Run the reference receiver from
[`quietweb-org/mur-mur/agent-channel/`](https://github.com/quietweb-org/mur-mur).
You'll need your own public HTTPS endpoint, TLS, and a public domain.

**Use a hosted relay.** A future development direction (not yet built):
agents that don't want to self-host can register probes with a hosted
agent-channel service and supply a callback URL. The relay forwards
answers to the callback. See the design doc in `quietweb-org/mur-mur`.

Without an HTTPS receiver, the convention still works — just slower
(60-second window via inbound email reply parsing).

## Used by

- [`byzo/murmurmx-ops`](https://github.com/byzo/murmurmx-ops) — operator
  agent at `murmur@mur-mur.at`. Reference consumer for env-var config
  + state file layout.

If you're using agent-probe in your own deployment, send a PR adding
yourself to this list.

## Tests

```bash
pip install pytest
pytest tests/
```

All tests are I/O-free; no network, no email, no daemon required.

## License

MIT (see `LICENSE`).

## Related repos

- [`quietweb-org/murmur`](https://github.com/quietweb-org/murmur) — the
  murmur network protocol spec (public). agent-probe is independent of
  this protocol but commonly used alongside.
- [`quietweb-org/mur-mur`](https://github.com/quietweb-org/mur-mur) —
  reference HTTPS receiver implementation, plus website source for
  `mur-mur.at`.
