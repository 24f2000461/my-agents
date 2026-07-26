# Safe AI Mailroom Agent

## ⚠️ Read this first
I could not produce a live public URL for you — my build environment has no
inbound internet access, only outbound access to package registries. You
need to deploy this yourself (instructions below, ~5 minutes on a free
host), then paste the resulting `https://.../v1/mailroom/actions` URL.

Also: the assignment doc you gave me had two empty sections —
**"Exact propose request and response"** and **"Exact commit request and
terminal response"** (dead links in the source). I built a contract from
the surrounding prose + the short transcript:

- `propose`: `{operation, evaluationId, dossiers:[{dossierId, ...}]}`
  → `{status:"awaiting_receipts", evaluationId, proposals:[{dossierId, callId,
    action, payload, evidence, proposalDigest}]}`
- `commit`: `{operation, receipts:[{evaluationId, dossierId, callId, receiptId,
  action, proposalDigest, verificationKey, approved}]}` → `{status:"completed",
  outcomes:[{evaluationId, dossierId, callId, receiptId, result, detail}]}`

  (I put `evaluationId` on each receipt, not at the top of the commit body,
  because that's what your transcript actually showed.)

**Before submitting, open the real assignment page and diff field names
against `app/schemas.py`.** Everything else (caching, replay, conflict,
safety logic) is independent of exact field names and should carry over
with small edits if names differ.

## What's implemented
- `app/canon.py` — canonical JSON + sha256 fingerprints; `callId` is derived
  from `(dossierId, content fingerprint)` so it's stable across evaluations
  and Checks, per the spec.
- `app/schemas.py` — pydantic schemas for the 6 allowed actions with strict,
  per-action payload fields. Anything the model returns that doesn't match
  is rejected before it can become a proposal.
- `app/llm.py` — the only place raw dossier content touches a model. Untrusted
  content goes in a delimited `<DATA>` block; the system prompt states
  explicitly that content is data, never instructions (lethal-trifecta
  separation: untrusted content, private/internal data, and outbound
  actions are never mixed in one unmediated channel — the model has no
  tool access at all, it only emits a JSON decision that your code then
  validates and, later, executes). Evidence quotes are capped at 300 chars
  total and validated, not trusted blindly.
- `app/store.py` — SQLite (WAL) durable state: dossier-decision cache keyed
  by `(dossierId, contentFingerprint)`, evaluations keyed by `evaluationId`
  with a hash of the exact dossier set, and receipts keyed by
  `(evaluationId, callId)` for idempotent replay.
- `app/main.py` — the endpoint:
  - malformed JSON / missing or unknown `operation` / duplicate dossier IDs
    → 400/422, **before** any model call.
  - same `evaluationId` + same dossier content → byte-identical replay of
    the persisted proposals, no new model call.
  - same `evaluationId` + changed dossier content → 409.
  - cache hit by content fingerprint → skip the model entirely (this is
    what makes Check's second evaluation and later Checks/Save free).
  - commit: rejects unknown `evaluationId`, unknown `callId`, and any
    action/digest mismatch against the persisted proposal, *before*
    executing any effect. Executing an already-committed receipt again
    returns `"replayed"` rather than re-applying the effect.
  - responses are canonical JSON, `Content-Type: application/json`, and
    bounded to 512 KiB.

## What you still need to decide
- **Model access**: set `ANTHROPIC_API_KEY` (and optionally `MAILROOM_MODEL`,
  default `claude-haiku-4-5-20251001`) as an env var on your host. Without a
  key it fails closed to `request_confirmation` rather than guessing — fine
  for testing the plumbing, but you need a real key for the graded run.
  Any OpenAI-compatible provider works too — just swap `_call_anthropic` in
  `app/llm.py`.
- **Persistent disk**: SQLite needs a real file across restarts. Render and
  Fly.io both offer a small free persistent volume — mount it at `/data`
  (matches `MAILROOM_DB_PATH` default in the Dockerfile).
- **`_execute_effect`** in `main.py` currently just records an auditable
  effect object rather than calling a real draft/CRM/mailer system, since
  the exam dossiers are synthetic and you were told never to add real
  credentials to a prompt. That's almost certainly what's graded (the
  receipt lifecycle + non-repetition), but re-read the grading rubric to
  confirm nothing further is expected here.

## Run locally
```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...     # optional for plumbing tests
export MAILROOM_DB_PATH=./mailroom.db
uvicorn app.main:app --reload --port 8080
```

## Test (no model calls, fully deterministic)
```bash
pip install pytest httpx
python3 tests/test_flow.py
```
Covers: malformed input, duplicate IDs, cache reuse across evaluations,
exact replay, changed-content conflict (409), receipt replay, and
digest-mismatch rejection.

## Deploy to get a public HTTPS URL

### Option A — Render.com (free tier, easiest)
1. Push this folder to a new GitHub repo.
2. On render.com: New → Web Service → connect the repo.
3. Environment: Docker (it'll detect the `Dockerfile`).
4. Add env var `ANTHROPIC_API_KEY`.
5. Add a free persistent disk, mount path `/data`.
6. Deploy. Your URL will be `https://<service>.onrender.com/v1/mailroom/actions`.

### Option B — Fly.io
```bash
fly launch --dockerfile Dockerfile --no-deploy
fly volumes create mailroom_data --size 1
# edit fly.toml: mounts = [{source="mailroom_data", destination="/data"}]
fly secrets set ANTHROPIC_API_KEY=sk-ant-...
fly deploy
```
URL: `https://<app>.fly.dev/v1/mailroom/actions`

### Option C — Railway.app
Connect the repo, add the `ANTHROPIC_API_KEY` var and a volume mounted at
`/data`, deploy. Railway gives you a `https://<app>.up.railway.app` URL.

Whichever you pick: **hit `GET https://<your-url>/healthz` first** to
confirm the service is actually reachable before pasting the `/v1/mailroom/actions`
URL into the grader.
