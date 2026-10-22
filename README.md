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
| Path confinement (`realpath`) | `../` traversal, absolute paths, symlinks leading out, a sibling such as `workspace-evil` |
| Commands as argv, no shell | commands off the list, commands given as a path (`/bin/sh`) |
| Per-command option list | options that carry a file: `--file=/etc/passwd`, `-f/etc/passwd` |
| Human approval per exact arguments | side effects without approval, approval reused, withdrawn or applied to other arguments |

Audit lines record the tool, the decision and the argument size, never argument values. An
approval and the call it allowed share a random `approval_id`. `run` needs no approval because
its list holds only commands that read (`grep`, `wc`).

[docs/acceptance-case.md](docs/acceptance-case.md) applies the boundary to one workflow, a
document-review assistant, including a reviewed document that carries planted instructions.
Tests: `python3 -m unittest tests.test_tool_boundary tests.test_review_workflow`.

Known limits: a path is checked before use, so a symlink swapped in between check and use
(time-of-check to time-of-use) or a hard link created inside the workspace is not caught
here; the workspace has to be a volume that holds nothing else. Every non-option command
argument is treated as a path, so a grep pattern such as `/etc` is refused.

## Status

Week 1 of a five-week build: network isolation and the gateway. Next: retrieval over public
documents (embeddings, vector store, search) inside the same network, then an agent with a
human approval step, then an infrastructure-as-code design for the same layout on AWS.

## How this was built

Built with an AI coding agent (Claude Code) from the owner's requirements: closed network,
zero cost, local models only. The owner reviews the code and reruns the checks before each
published result.
