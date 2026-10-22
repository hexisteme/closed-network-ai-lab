# closed-network-ai-lab

A local, zero-cost stand-in for a private-network AI environment: the pattern a regulated
bank uses when model traffic must never touch the public internet.

| Cloud pattern | This lab |
|---|---|
| Private subnet with no internet route | Docker `internal` network `lab_internal` |
| VPC interface endpoint to the model service | `model-gateway`, the only service on both networks |
| Endpoint policy | Path allowlist (`/api/chat`, `/api/embed`, read-only `/api/tags`, `/api/version`) and model allowlist |
| Data-exfiltration guard | Model downloads and cloud-routed models are refused |
| Access logging | One JSONL audit line per request (sizes and decision, never prompt bodies) |

Models run on the host through [Ollama](https://ollama.com): `qwen3:4b` and `gemma3:4b` for chat,
`bge-m3` for embeddings. The gateway and the check use the Python standard library only, so
the containers install nothing.

## Run

```
docker compose up -d model-gateway
docker compose run --rm egress-check
docker compose down
```

`egress-check` runs inside `lab_internal` and writes `evidence/egress-check-<UTC>.json`.
It passes only if all of these hold:

1. TCP to `1.1.1.1:443` and HTTPS to `example.com` fail (no internet).
2. Direct TCP to the host model API fails (the gateway cannot be bypassed).
3. Through the gateway: `/api/tags` lists no cloud models, `/api/pull` is refused,
   a cloud model is refused, and both local chat models and the embedding model answer.

A chat answer counts only if it finished (`done_reason` is `stop`) and carries no reasoning
text; a reasoning model's thinking must arrive separately in `message.thinking`.

Unit tests for the gateway policy and the answer check: `python3 -m unittest discover -s tests`.

## Latest result

`evidence/egress-check-20261018T071515Z.json`: 10 of 10 checks passed (Mac mini M4, 24 GB,
Docker Desktop 29.6.1, Ollama 0.32.14).

| Check | Observed |
|---|---|
| Internet by IP / HTTPS | `Network is unreachable` / name resolution fails |
| Host model API, direct | name resolution fails (IP routes outside the subnet are covered by the first check) |
| Gateway model listing | `bge-m3:latest`, `gemma3:4b`, `qwen3:4b` only |
| `/api/pull`, cloud model | 403 `path_not_allowed`, 403 `cloud_model_blocked` |
| Chat (first call, includes model load) | qwen3:4b `ready` in 10.6 s, 667 characters of thinking kept apart · gemma3:4b `Ready` in 7.2 s |
| Embed (first call, includes model load) | bge-m3 52.9 s, 1024 dimensions |

## Fixed: reasoning text in the answer

The 2026-10-17 run passed while `qwen3:4b` answered `Hmm, the user just asked me to reply
with one word: "ready".` The cause, reproduced against Ollama 0.32.14 directly:

| Request | qwen3:4b | gemma3:4b |
|---|---|---|
| `"think": false` | still reasons; Ollama stops parsing, so reasoning, `</think>` and the answer all land in `content` (cut off at 16 tokens) | answers |
| `"think": true` | answer in `content`, reasoning in `thinking` | 400 `does not support thinking` |
| `think` left out | answer in `content`, reasoning in `thinking` | answers |

The check now leaves `think` out, gives the reasoning 512 tokens to finish, and fails an
answer that was truncated or contains `<think>`/`</think>`. Replayed through the new check,
the 2026-10-17 qwen3 result fails as `answer_truncated`.

## Tool boundary (groundwork for the agent)

`agent/tool_boundary.py` decides each tool call a model makes before anything runs. A JSON
schema only fixes an argument's shape: `"type": "string"` accepts `../outside.txt` just as
readily as `notes.txt`. The boundary checks what the schema cannot express:

| Check | Refuses |
|---|---|
| Tool list and strict schema | unknown tools, missing or extra arguments, wrong types |
| Path confinement (`realpath`) | any path that resolves outside the workspace: `../` traversal or an absolute path leading out, symlinks leading out, a sibling such as `workspace-evil` |
| Commands as argv, no shell | commands off the list, commands given as a path (`/bin/sh`) |
| Per-command option list | options that carry a file: `--file=/etc/passwd`, `-f/etc/passwd` |
| Human approval per exact arguments | side effects without approval, approval reused, withdrawn or applied to other arguments |

Audit lines record the tool, the decision and the argument size, never argument values. An
approval and the call it allowed share a random `approval_id`. `run` needs no approval because
its list holds only commands that read (`grep`, `wc`).

### Execution and the audit log file

`agent/tool_session.py` runs only what the boundary allowed and appends one JSONL line per
approval, withdrawal, decision and execution. A decision and its execution share a random
`call_id`.

| What could go wrong after the check | What the session does |
|---|---|
| A path now resolves to another file | Resolves each path as the boundary did and refuses it if the result changed since the check |
| A file or directory is swapped for a symlink | Opens the workspace once per session, then the resolved path one component at a time with `O_NOFOLLOW` from that descriptor, never through `..` |
| A hard link points at a file outside | Reads and writes only regular files with one link; a write truncates after that check |
| `grep` or `wc` opens a swapped path itself, or reads a file name as an option | Opens the file operands itself and hands them over as `/dev/fd/N`, linked in a fresh private directory; runs `program options -- operands` with each file named `./<path in the workspace>` |
| A shell, the caller's environment or stdin reach the program | argv list from `/usr/bin:/bin`, no shell, `PATH` and `LC_ALL` only, no stdin, 10 s limit, output cut while it is read |
| A session is used after it closed | Every call, approval and withdrawal is refused, so the workspace is never opened again by path |

`agent/audit_check.py` reads a log file back. Every line must be canonical JSON without duplicate
fields, and every field an enumerated word, a count, a timestamp or a random id of fixed form, so
no line can carry an argument value as text. A count or an id could still encode one: the check
cannot tell where a number came from. It also checks that each approval is used once or
withdrawn, that every side effect had an approval and nothing else carried one, that an unlisted
tool was only ever denied, and that every allowed call ran exactly once while no recorded
execution lacks an allow. It checks the recorded events against each other and cannot show that
the log is complete: an action that was never logged leaves nothing to check. Run it on the log of a closed session; it fails on any violation and on any approval
left open: `python3 agent/audit_check.py <log> [argument values…]`. Argument values shorter than
four characters are not searched for, only counted.

[docs/acceptance-case.md](docs/acceptance-case.md) applies all of this to one workflow, a
document-review assistant, including a reviewed document that carries planted instructions.

```
docker compose --profile demo run --rm review-demo
```

runs that case end to end with scripted approvals, in a container with no network, no root, no
capabilities and a read-only image, and writes `evidence/review-demo-<UTC>-<random>.json` and the
audit log next to it, both created new. Tests: `python3 -m unittest discover -s tests`.

Known limits: a directory inside the workspace that another process moves out of it while a call
walks through it is not caught, so the workspace still has to be a volume nothing else writes
to. The programs `run` starts are trusted to only read what they are given. Every non-option
command argument is checked as a path, so a grep pattern such as `/etc` is refused, and a `run`
call may name each file only once. An argument that starts with `-` before any `--` is an option
wherever it stands, so `wc notes.txt -l` counts lines.

## Status

Done: network isolation and the gateway (week 1); the tool boundary with execution, an audit log
file and the document-review acceptance case. Next: retrieval over public documents (embeddings,
vector store, search) inside the same network, then a model-driven agent that calls tools through
this boundary, then an infrastructure-as-code design for the same layout on AWS.

## How this was built

Built with an AI coding agent (Claude Code) from the owner's requirements: closed network,
zero cost, local models only. The owner reviews the code and reruns the checks before each
published result.

## License

MIT. See [LICENSE](LICENSE).
