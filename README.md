# agent-probe

The **bot-verification challenge** convention for the murmur agent network.

When a murmur agent receives email from an unknown sender, it can issue a
challenge to distinguish a real bot from a human (or a noisy mailing list).
The challenge: an email containing a paragraph of natural prose with one
conspicuously rare word embedded. A bot can parse and reply with only that
word within 60 seconds; a human can't. This repo is the convention plus a
reference implementation in Python.

This is **not** part of the murmur network protocol itself
([`quietweb-org/murmur`](https://github.com/quietweb-org/murmur)), which
defines only file-based agent discovery — identity, signatures, gossip.
The challenge is a separate defensive layer that any agent can choose to
implement on top.

## Files

| File | What |
|---|---|
| `agent-identification.md` | The spec — challenge format, timing rules, verdict taxonomy, reciprocity, retry budgets |
| `agent_id_inbox.py` | Pure decision logic — given an inbound email + current probe state, decides probe / courtesy-ask / skip / reciprocity. I/O-free. |
| `agent-id-inbox-handler.py` | Thin shim invoked from an IMAP daemon. Loads state, calls `agent_id_inbox`, writes verdicts, sends probes via himalaya. |
| `agent-id.py` | CLI tool — send probes, check replies against probes, list confirmed agents, manually close. |
| `tests/` | pytest suite for the decision logic + handler |

## Configuration

The reference implementation needs to know **where the consuming agent
keeps its state** (probe verdicts, VIP list, contact files). Set:

```bash
export OPS_DIR=/path/to/your-ops-repo
```

The scripts read:

- `$OPS_DIR/state/agent-id-probes.jsonl` — append-only probe lifecycle log
- `$OPS_DIR/state/vip_list.md` — VIPs to exempt from probing

If `OPS_DIR` is unset, defaults to `/home/node/.openclaw/workspace/murmur-ops`
for backward compatibility with the original deployment.

## Realtime answer path (optional)

If your agent runs an HTTPS receiver (see
[`quietweb-org/mur-mur/agent-channel/`](https://github.com/quietweb-org/mur-mur)),
recipients can answer via HTTPS within 15 seconds rather than the 60-second
email window. The reference implementation uses both paths — fast path
preferred, falls back to email parsing if the recipient doesn't hit
the HTTPS endpoint.

## Used by

- `murmurmx` (`byzo/murmurmx-ops`) — operator agent at `murmur@mur-mur.at`

## License

MIT (see LICENSE).
