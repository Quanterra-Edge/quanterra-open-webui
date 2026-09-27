# Quanterra Open WebUI

This repository is a fork of [open-webui/open-webui](https://github.com/open-webui/open-webui),
customized for the Quanterra hosted runtimes. It replaces the former thin-image
repository (`quanterra-open-webui-image`), whose add-on Functions could not change
the chat screen itself.

## Where Quanterra code lives

| Path                                    | Purpose                                                                                 |
| --------------------------------------- | --------------------------------------------------------------------------------------- |
| `backend/open_webui/quanterra/`         | Backend package: routes under `/api/v1/quanterra`, runtime discovery, Responses helpers |
| `src/lib/components/quanterra/`         | Svelte components for the Quanterra panels                                              |
| `quanterra/`                            | This documentation and `touch-points.txt`                                               |
| `scripts/quanterra/`                    | Guard and maintenance scripts                                                           |
| `.github/workflows/quanterra-image.yml` | Our CI: guard, build, publish, smoke                                                    |

Everything else is upstream. The few upstream files that carry a Quanterra change
are listed in `quanterra/touch-points.txt`; `scripts/quanterra/check_touch_points.py`
fails when a change lands anywhere else. Keep each touch point to a handful of lines.

The upstream base is recorded in `backend/open_webui/quanterra/version.py`
(`UPSTREAM_TAG`). Upstream workflows that publish to ghcr.io, PyPI or GitHub releases,
label issues, or need upstream secrets are kept under a `.disabled` name so they do
not run here; upstream's own `backend.yaml` (ruff) and `frontend.yaml` (format, i18n,
build, unit tests) still run.

## Taking a new upstream release

```bash
git remote add upstream https://github.com/open-webui/open-webui.git   # once
git fetch upstream tag v0.11.5 --no-tags
git checkout -b chore/upstream-v0.11.5 main
git merge v0.11.5
# resolve conflicts; they can only be in the files listed in quanterra/touch-points.txt
# set UPSTREAM_TAG = 'v0.11.5' in backend/open_webui/quanterra/version.py
git push origin v0.11.5
python scripts/quanterra/check_touch_points.py
```

Read the upstream release notes for database migrations (back up the data volume
before the first start of a new version) and for changes to the OpenAI/Responses
connection code, which the Quanterra runtime integration relies on.

## Local development

Same as upstream: `npm install --force` and `npm run dev` for the front end;
`cd backend`, `pip install -r requirements.txt`, `sh dev.sh` for the back end
(Python 3.11). Run the guard before pushing:

```bash
python scripts/quanterra/check_touch_points.py
```

## Image and deployment

CI builds the standard (non-slim) upstream `Dockerfile` for `linux/amd64` and pushes
`<ACR_REGISTRY>/<OPENWEBUI_IMAGE_NAME>` with the tags `<branch>` and
`sha-<commit>`, then smoke-tests the pushed digest. Repository variables:
`ACR_REGISTRY`, `OPENWEBUI_IMAGE_NAME`; secrets: `ACR_USERNAME`, `ACR_PASSWORD`.

The Quanterra control plane deploys this image through its "Deploy Frontend" wizard
(template `frontend-open-webui`, `FRONTEND_IMAGE` = a published tag or digest).
The compose template the wizard uses lives in the control-plane repository under
`deploy/templates/open-webui/docker-compose.yml`.

## Conversation continuity on Quanterra connections

Upstream replays the whole chat on every Responses call, tool items included
(`routers/openai.py` `convert_to_responses_payload`). The hosted runtime's official
parser accepts `message` items only, so a chat whose history holds one server-side
tool round (skills, harness, MCP, `http_request`) would answer 400 on every later
turn. `backend/open_webui/quanterra/responses.py` (hooked from `routers/openai.py`,
guard in `utils/middleware.py`) changes what a connection tagged `quanterra` receives;
every other provider is untouched.

Per call (a connection tagged `quanterra` with `api_type: responses`; a tagged
Chat Completions connection is left as upstream sends it):

| Call                                                                | Body sent to `POST <base_url>/responses`                                                                                                                                                                       |
| ------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| First chat turn on a (chat, model): the conversation is created now | The chat history as plain `message` items (seeds the empty conversation), `conversation_id`, `model`, `stream`, `instructions` when a system prompt is set. No `tools`, no `tool_choice`, tool items stripped. |
| Later chat turn (no `metadata.task`, turn ends with a user message) | `input` = the new user message only (text, images, files as the converter builds them), `conversation_id`, same keys otherwise.                                                                                |
| Task call (title, tags, follow-ups, memory, query generation)       | Stateless: the converter's messages as plain `message` items, no `conversation_id`; `function_call`, `function_call_output` and `reasoning` items are stripped.                                                |
| Continue, or a turn the conversation cannot take                    | Stateless, same shape as a task call (history as messages, tool items stripped).                                                                                                                               |

Conversations: one per (chat, model). The first chat turn does
`POST <base_url>/responses/conversations` with the same headers as the chat call
(Bearer from the `system_oauth` session; the `x-quenterra-thread-id` = chat id
header comes from the discovery connection config, `headers: {x-quenterra-thread-id: "{{CHAT_ID}}"}`)
and stores the id in the chat's `meta` as `meta.quanterra.conversations[<model id>]`,
so it survives restarts. The meta is read and written for the signed-in user's
own chat only; another user's chat (an admin continuing it) stays stateless.
The runtime's conflicts are told apart: a transient 409 (the previous turn of the
chat is still streaming or aborting, an idempotency replay) re-sends the turn
stateless and keeps the stored id; a conversation the runtime does not know
(409 `unknown continuation id for this caller`, 410) is replaced once and the
history re-sent on the new one. When creation fails the turn goes out stateless
so the chat still works. Open WebUI's own tool loop does not run for these
models, nor for a workspace model built on one (the guard resolves the base
model): the runtime returns matched `function_call`/`function_call_output`
pairs, and a dangling call is never executed client-side.

Limitations: the runtime conversation is linear. Regenerating or editing an
earlier message re-sends that message on the conversation, which continues from
the latest turn; the runtime does not rewind. Temporary chats and channels have
no saved meta and stay stateless.

## Roadmap

1. Foundation (this layout, CI, guard) — done.
2. Pick a hosted agent when starting a new chat: runtimes discovered live from the
   control plane, one model entry per hosted agent.
3. Quanterra chat panel: agent capabilities, harness todos and background tasks,
   compaction, token and context usage.
