# Teaching notes condenser

Send raw class notes to a Telegram bot. It condenses them with Gemini 3.8 Flash
on Vertex AI and adds a row to your Notion database: title (month and day, e.g.
"July 16"), today's date, the raw notes, and the condensed entry as the page body.

A second, independent pipeline mirrors that Notion database into a Google Doc on
a schedule, so it can be used as a live NotebookLM source — ask questions about
your classes, spot patterns across a term, plan the next one.

```
raw notes -> Telegram bot -> Lambda (Vertex condense + write to Notion) -> Notion row
Notion DB  -> Lambda (scheduled, reads + renders) -> Google Doc -> NotebookLM (auto-synced)
```

Nothing connects the two pipelines — the Drive sync only ever reads Notion. It
also picks up notes you edit directly in Notion, not just ones the bot wrote.

## Architecture

```
You (Telegram)
      │  send raw notes as one or more messages, then /done
      ▼
Telegram Bot API
      │  webhook POST on every message
      ▼
AWS Lambda  (single function — app/handler.py orchestrates everything below)
      │
      ├─ buffers messages & dedupes retried webhooks ──▶ DynamoDB
      ├─ condenses the notes on /done ─────────────────▶ Vertex AI (Gemini)
      └─ writes the finished entry ────────────────────▶ Notion API
                                                                │
                                                                ▼
                                                  New row in your Notion database
```

| Piece             | Role                                                              | File                |
|-------------------|-------------------------------------------------------------------|---------------------|
| Telegram Bot      | your only interface — send notes, get a confirmation + link back  | `app/telegram.py`   |
| AWS Lambda        | receives the webhook, routes commands, orchestrates the pipeline  | `app/handler.py`    |
| DynamoDB          | holds a chat's buffered messages until `/done`; dedupes retries    | `app/buffer.py`     |
| Vertex AI (Gemini) | runs the condensing prompt over the raw notes                    | `app/condense.py`, `app/prompt.txt` |
| Notion API        | creates the row: title, date, condensed + raw text                | `app/notion.py`     |

Everything runs on request — there's no server to keep up. Lambda + DynamoDB
only cost anything while actually processing a message, which for this
use case (a few classes a week) is effectively free.

### Notion → Drive → NotebookLM (second pipeline)

```
EventBridge Scheduler (weekends, every 6h, IST + one Monday 00:00 catch-up)
      │
      ▼
AWS Lambda  (app/drive_sync.py)
      │
      ├─ query every row, oldest first ──────────▶ Notion API (data source query)
      ├─ fetch each page's condensed body ───────▶ Notion API (GET .../markdown)
      ├─ render one markdown document
      ├─ hash it, skip the Drive write if unchanged since last run
      └─ overwrite the whole Doc's content ──────▶ Google Drive API (multipart update)
                                                          │
                                                          ▼
                                          Google Doc, same file ID every run
                                                          │
                                                          ▼ (automatic — no click)
                                                     NotebookLM source
```

| Piece              | Role                                                             | File                  |
|---------------------|-------------------------------------------------------------------|-----------------------|
| EventBridge Scheduler | fires the sync Lambda on a cron, in `Asia/Kolkata`               | `template.yaml` (`ScheduleV2` events) |
| AWS Lambda          | reads Notion, renders the Doc, writes Drive, tracks the last hash | `app/drive_sync.py`   |
| Notion API (read)   | data source query + per-page markdown fetch                      | `app/notion_read.py`  |
| Google Drive API    | overwrites the target Doc's full content on each change           | `app/drive.py`        |
| DynamoDB            | stores the SHA-256 of the last Doc content written (`sync#drive` item) | `app/buffer.py`  |
| NotebookLM          | auto-syncs the Doc once added as a source — no manual re-add ever | (Google's UI, one-time) |

**Why a full overwrite every run, not an incremental append**: the sync has no
per-page state beyond one content hash. Every run re-reads the entire Notion
database and re-renders the whole Doc from scratch, so edits, deletions, and
reorders in Notion all show up correctly — there is nothing that can drift out
of sync. The hash exists only so an unchanged run skips the Drive write
(so NotebookLM doesn't re-index the Doc for no reason), not to decide what
content to send.

**Why NotebookLM needs no maintenance after the one-time setup**: Google
shipped automatic Drive syncing for NotebookLM in May 2026 — native Google
Docs/Sheets/Slides sources refresh inside a notebook whenever the underlying
Drive file changes, with no sync button and no setting to turn on. That's the
whole reason the target is a **Google Doc** and not a plain `.md`/`.txt`/PDF
file in Drive — only native Workspace files get this treatment.

## Prerequisites

1. **Telegram bot** — message `@BotFather` → `/newbot` → copy the bot token.
   Message `@userinfobot` to get your own numeric Telegram user id.
2. **Notion integration** — [notion.so/my-integrations](https://notion.so/my-integrations)
   → New internal integration → copy the secret token.
3. **Share the database with the integration** — open the "Primary batch notes"
   database in Notion → `...` menu → Connections → add your integration.
   Without this the API returns 404.
4. **AWS account, CLI configured** (see 4a below).
5. **AWS SAM CLI** installed: `brew install awscli aws-sam-cli`.
6. **A billing-enabled GCP project with Vertex AI**, federated to the Lambda's
   IAM role via Workload Identity Federation (see 4b below). There is no API
   key anywhere in this setup.

### 4a. Set up AWS CLI credentials (first time only, per AWS account)

Don't use your root account login for the CLI. Instead:
- AWS Console → IAM → Users → Create user → attach `AdministratorAccess`
  (fine for a personal project; tighten later if you want).
- That user → Security credentials → Create access key → choose
  "Command Line Interface (CLI)" → copy the Access Key ID + Secret Access Key
  (shown only once).
- Run `aws configure` locally and paste them in, along with a default region
  (e.g. `us-west-1`) and output format `json`.

### 4b. Federate the Lambda into GCP (Workload Identity Federation)

The model runs on **Vertex AI** (`aiplatform.googleapis.com`) — the one piece of
infrastructure that lives outside AWS. Two deliberate choices here:

- **Vertex, not the Gemini Developer API.** The Developer API bills against an
  AI Studio *prepayment* balance — a separate purse that Cloud Billing credits
  can never fund. When that balance is empty every call 429s with
  `RESOURCE_EXHAUSTED`, regardless of project credits. Vertex bills through
  Cloud Billing, so credits apply. Vertex also doesn't use prompts to improve
  Google's products — which matters for notes about real students.
- **Workload Identity Federation, not a service-account key.** GCP trusts the
  Lambda execution role directly via STS token exchange. Nothing long-lived is
  stored on the AWS side; there is no key to leak or rotate.

One-time setup (values for the current deployment shown; adjust ids to yours):

```bash
P=<gcp-project-id>
gcloud services enable aiplatform.googleapis.com sts.googleapis.com \
  iamcredentials.googleapis.com --project=$P

# Service account the Lambda will impersonate; only needs Vertex access.
gcloud iam service-accounts create teaching-notes-vertex --project=$P
gcloud projects add-iam-policy-binding $P \
  --member="serviceAccount:teaching-notes-vertex@${P}.iam.gserviceaccount.com" \
  --role=roles/aiplatform.user

# Pool + AWS provider, locked to the Lambda's execution role.
# google.subject maps to the extracted role NAME, not the full assumed-role ARN:
# mapped attributes cap at 127 bytes and the full ARN (role name + function
# name) exceeds it — STS then rejects every exchange.
ROLE=<lambda-execution-role-name>   # e.g. teaching-notes-condenser-CondenserFunctionRole-...
gcloud iam workload-identity-pools create aws-lambda-pool \
  --location=global --project=$P --display-name="AWS Lambda"
gcloud iam workload-identity-pools providers create-aws teaching-notes-condenser \
  --location=global --project=$P --workload-identity-pool=aws-lambda-pool \
  --account-id=<aws-account-id> \
  --attribute-mapping="google.subject=assertion.arn.extract('assumed-role/{role}/'),attribute.aws_role=assertion.arn.extract('assumed-role/{role}/')" \
  --attribute-condition="attribute.aws_role=='${ROLE}'"

# Let the federated Lambda role impersonate the service account.
PROJECT_NUMBER=$(gcloud projects describe $P --format='value(projectNumber)')
gcloud iam service-accounts add-iam-policy-binding \
  "teaching-notes-vertex@${P}.iam.gserviceaccount.com" --project=$P \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/aws-lambda-pool/attribute.aws_role/${ROLE}"

# Credential config the google-auth library reads at runtime. Contains only
# pool/provider/service-account references — no secret — so it is committed
# and ships inside the deploy package.
gcloud iam workload-identity-pools create-cred-config \
  "projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/aws-lambda-pool/providers/teaching-notes-condenser" \
  --service-account="teaching-notes-vertex@${P}.iam.gserviceaccount.com" \
  --aws --output-file=app/gcp-wif-credentials.json
```

How it works at runtime: `google-auth` reads `app/gcp-wif-credentials.json`
(via `GOOGLE_APPLICATION_CREDENTIALS`), signs an AWS `GetCallerIdentity`
request with the Lambda role's own credentials from the `AWS_*` env vars
(no metadata server needed — the library handles Lambda explicitly), exchanges
it with GCP STS, and impersonates `teaching-notes-vertex`.

## NotebookLM sync setup (one-time, do before deploying the sync)

The Notion→Drive→NotebookLM pipeline needs five one-time steps outside this
repo, done in order:

1. **Enable the Drive API** on the same GCP project Vertex already runs in:
   ```bash
   gcloud services enable drive.googleapis.com --project=<gcp-project-id>
   ```
2. **Create the Google Doc yourself** — Drive → New → Google Doc, leave it
   empty, name it whatever you want to see in NotebookLM (e.g.
   "Teaching Notes — Primary Batch"). It must be owned by a real Google
   account, not the service account: service accounts on a consumer Gmail
   account have no usable Drive storage, and a NotebookLM source has to be a
   file you can see in your own Drive picker. Copy the file ID from the URL
   (`docs.google.com/document/d/<FILE_ID>/edit`) — it's the `DriveDocId`
   stack parameter.
3. **Share that Doc with the service account as Editor** — Share →
   `teaching-notes-vertex@<gcp-project-id>.iam.gserviceaccount.com` → Editor →
   uncheck "Notify people". This is where the sync Lambda's write access comes
   from — it is a Drive-level permission, not a GCP IAM role, so nothing in
   4b grants it.
4. **Turn on "Read content" on the Notion integration** — the integration used
   by `app/notion.py` has so far only ever written pages. notion.so →
   Settings → Connections → your integration → Capabilities → check
   **Read content**. Without this, `GET /pages/{id}/markdown` 403s.
5. **Extend the Workload Identity Federation trust to the second Lambda's own
   execution role.** SAM generates a separate IAM role per function
   (`DriveSyncFunctionRole`, distinct from `CondenserFunctionRole`), and both
   the WIF provider's `--attribute-condition` and the service account's
   `workloadIdentityUser` binding from step 4b are pinned to one specific role
   name. A second Lambda calling Google fails at both checks until both are
   widened to also allow the new role. Get the actual role name after your
   first `sam deploy` creates it:
   ```bash
   aws cloudformation describe-stack-resources --stack-name <your-stack-name> \
     --query "StackResources[?LogicalResourceId=='DriveSyncFunctionRole'].PhysicalResourceId" \
     --output text
   ```
   Then widen both checks to an OR of both role names (do **not** widen to a
   whole-pool or prefix match — that would let *any* future Lambda in this
   account impersonate the Vertex/Drive service account with no extra step):
   ```bash
   P=<gcp-project-id>
   PROJECT_NUMBER=$(gcloud projects describe $P --format='value(projectNumber)')
   COND_ROLE=teaching-notes-condenser-CondenserFunctionRole-...   # from 4b
   SYNC_ROLE=teaching-notes-condenser-DriveSyncFunctionRole-...   # from above

   gcloud iam workload-identity-pools providers update-aws teaching-notes-condenser \
     --location=global --project=$P --workload-identity-pool=aws-lambda-pool \
     --attribute-condition="attribute.aws_role=='${COND_ROLE}' || attribute.aws_role=='${SYNC_ROLE}'"

   gcloud iam service-accounts add-iam-policy-binding \
     "teaching-notes-vertex@${P}.iam.gserviceaccount.com" --project=$P \
     --role=roles/iam.workloadIdentityUser \
     --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/aws-lambda-pool/attribute.aws_role/${SYNC_ROLE}"
   ```
   IAM policy changes on a service account can take a minute or two to
   propagate — a `RefreshError` / `getAccessToken denied` right after running
   this is expected transiently; retry after a short wait before assuming
   something is actually wrong. This is the same class of gotcha as the
   role-name pinning documented in Troubleshooting below, now doubled because
   there are two roles to keep in sync instead of one.
6. **Add the Doc to NotebookLM only after the first successful sync** (see
   Deploy, below) — an empty Doc added as a source indexes as empty, and you'd
   otherwise be relying on auto-sync to pick up the very first write.

## Confirm the Notion database (before deploying)

Use the **data source id** (settings menu → Manage data sources → Copy data
source ID) — the same value the stack's `NotionDataSourceId` parameter takes:

```bash
export NOTION_TOKEN=ntn_...
curl -s https://api.notion.com/v1/data_sources/39a3ef88-7ec8-80fc-a046-000b767839f1 \
  -H "Authorization: Bearer $NOTION_TOKEN" \
  -H "Notion-Version: 2026-03-11" | python3 -m json.tool
```

Expect `properties` to contain `Name`, `Date`, `Raw notes`.
If you get a 404, redo the "share with integration" step.

## Deploy

```bash
sam build --use-container
sam deploy --guided
```

`--use-container` is not optional: `google-genai` pulls in `pydantic`, whose
core is a compiled extension. A plain build on macOS can resolve macOS wheels
that fail with an ImportError inside Lambda's Linux runtime. The container
build produces matching `manylinux` x86_64 wheels (needs Docker running).

You'll be prompted for the stack parameters:
- `TelegramBotToken`
- `TelegramWebhookSecret` — make up a random string (e.g. `openssl rand -hex 20`)
- `AllowedChatId` — your Telegram user id
- `NotionToken`
- `NotionDataSourceId` — in Notion, open the database's settings menu, then
  Manage data sources -> Copy data source ID
- `VertexProject` — the GCP project id from step 4b above
- `VertexLocation` — defaults to `global`, which sidesteps per-region model
  availability checks
- `GeminiModelId` — defaults to `gemini-3.8-flash`
- `LocalTz` — defaults to `Asia/Kolkata`; change if you're not in that timezone
  (used to compute the Date field correctly — Lambda runs in UTC)
- `DriveDocId` — the Google Doc file ID from step 2 of "NotebookLM sync setup"
  above
- `NotionStudentsProperty` — defaults to `Multi-select`, the unrenamed default
  name of the multi-select property on this database. Only change this if you
  rename that property in Notion.

After the first `--guided` run, subsequent deploys (e.g. after a code change)
just need `sam build && sam deploy` — your answers are saved to
`samconfig.toml` (gitignored, since it's per-machine and SAM writes some
parameter values there in plaintext).

Note the `FunctionUrl` output — you need it next.

## Register the Telegram webhook

```bash
curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" \
  -d "url=<FunctionUrl output from sam deploy>" \
  -d "secret_token=<the TelegramWebhookSecret you chose>"

curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getWebhookInfo"
```

The second command should show your URL with `pending_update_count: 0` and no
`last_error_message`.

## Using it

1. Message your bot on Telegram with your raw class notes (split across as many
   messages as you like — Telegram caps a single message at 4096 characters).
2. Send `/done`.
3. The bot replies `✅ Added to Notion` with a link once the row is created.

Other commands: `/start` or `/help` for usage instructions, `/quit` to discard
the buffered notes and start over.

### NotebookLM

The Doc syncs on its own — every 6h on weekends (IST), plus a Monday 00:00
catch-up for anything written after the last weekend poll. Add the Doc to a
NotebookLM notebook once (notebook → Add source → Google Drive → pick it);
after that, NotebookLM's automatic Drive sync keeps it current with no further
action on either side.

To force an immediate sync instead of waiting for the schedule (e.g. right
after a class, or to verify the pipeline after a change):
```bash
aws lambda invoke --function-name <DriveSyncFunctionName output> \
  --region us-west-1 --cli-read-timeout 300 /dev/stdout
```
Expect `{"changed": true, "sessions": N}` if the Notion data changed since the
last sync, or `{"changed": false, "sessions": N}` if not — either is a
successful run, distinguished only by whether the Doc was actually rewritten.

Weekday edits to Notion wait until Saturday to reach the Doc — a deliberate
tradeoff to keep the schedule to 9 invocations/week. The command above is the
escape hatch when that lag matters.

## Troubleshooting

**`sam build` fails with a Python version error**, even though `python3
--version` correctly shows 3.12 in your shell (e.g. via `pyenv`):
```
PythonPipBuilder:Validation - Binary validation failed for python, searched
for python in following locations: ['/usr/local/bin/python3', '/usr/bin/python3']
which did not satisfy constraints for runtime: python3.12.
```
SAM's builder only checks those two hardcoded paths, not your `PATH`/pyenv
shims. Two fixes:
- **Build in a container (recommended)** — sidesteps the issue entirely by
  building inside an official Lambda Python 3.12 image:
  ```bash
  sam build --use-container   # needs Docker Desktop installed and running
  ```
- **Or symlink a Homebrew Python 3.12** into one of the expected paths:
  ```bash
  brew install python@3.12
  sudo ln -sf "$(brew --prefix python@3.12)/bin/python3.12" /usr/local/bin/python3
  ```

**`OAuthError: ... google.subject exceeds the 127 bytes limit`** — the WIF
provider's attribute mapping uses the full assumed-role ARN as `google.subject`.
The ARN (role name + function name, both CloudFormation-generated and long)
blows past STS's 127-byte cap on mapped attributes. Fix the mapping to the
extracted role name, as in 4b:
```bash
gcloud iam workload-identity-pools providers update-aws teaching-notes-condenser \
  --location=global --project=<PROJECT_ID> --workload-identity-pool=aws-lambda-pool \
  --attribute-mapping="google.subject=assertion.arn.extract('assumed-role/{role}/'),attribute.aws_role=assertion.arn.extract('assumed-role/{role}/')"
```

**Vertex calls suddenly denied after deleting/recreating the CloudFormation
stack** — the WIF provider's attribute condition pins the exact Lambda
execution role name, and CloudFormation generates a fresh random suffix on
recreate (the current one ends `-tlNjjmhAtJ6p`). Update the provider's
`--attribute-condition` and the service account's `workloadIdentityUser`
binding to the new role name (both commands in 4b). A plain `sam deploy`
update does not change the role name — only delete + recreate does.

**The bot replies with a generic error** — get the real traceback:
```bash
sam logs --stack-name <your-stack-name> --tail
```
(find `<your-stack-name>` with `grep stack_name samconfig.toml`) then trigger
the bot again while it's tailing.

## Verification checklist

- [ ] Notion access curl above returns the 3 expected properties.
- [ ] `sam deploy` succeeds and prints a `FunctionUrl`.
- [ ] `getWebhookInfo` shows no `last_error_message`.
- [ ] End-to-end: send 2-3 messages of real notes + `/done` → bot confirms and a
      new Notion row appears with today's date, a month-and-day title (e.g.
      "July 16"), verbatim raw notes, and a condensed entry in the 6-section
      format.
- [ ] Security: POST to the Function URL without the `X-Telegram-Bot-Api-Secret-Token`
      header → should be silently ignored (200, no Notion row). Message the bot
      from a different Telegram account → "Not authorized", no row.

Notion→Drive→NotebookLM pipeline:
- [ ] Manual invoke (see "NotebookLM" above) returns `{"changed": true, ...}`
      on first run, with `sessions` matching the row count in Notion.
- [ ] The Doc shows a real Heading 1 title, one real Heading 2 per session in
      date order, bold section labels, and a horizontal rule between sessions
      — not literal `#`/`**`/`---` characters.
- [ ] Invoking again immediately, with no Notion changes, returns
      `{"changed": false, ...}` — confirms the hash gate isn't spuriously
      rewriting the Doc (and thus not spuriously re-triggering NotebookLM's
      re-index) on every scheduled run.
- [ ] Edit a note directly in Notion (not via the bot) → invoke → edit shows
      up in the Doc. Archive a row in Notion → invoke → that session's block
      disappears from the Doc. Both confirm the full-regenerate design, not
      just append.
- [ ] `aws scheduler list-schedules --region us-west-1` shows two schedules
      targeting `DriveSyncFunction`, both with `Asia/Kolkata` as the timezone.
- [ ] After adding the Doc as a NotebookLM source, ask it something that
      depends on freshly-synced content and confirm the answer reflects it.
      Google publishes no SLA for auto-sync propagation, so give it a few
      minutes; NotebookLM's own manual per-source refresh works as a nudge.

## Notes on the design

- **Buffering**: raw notes often exceed Telegram's 4096-char message cap, so
  plain-text messages are buffered per chat in DynamoDB until you send `/done`
  (or discarded with `/quit`). Buffers auto-expire after 6 hours if abandoned.
- **Idempotency**: Telegram retries the webhook if it doesn't get a fast 200.
  Each `update_id` is recorded in DynamoDB (1h TTL) so a slow Gemini call never
  creates a duplicate Notion row.
- **Chunking**: Notion caps a single rich_text object at 2000 characters, so long
  raw notes are split into multiple rich_text chunks in the `Raw notes` property
  (at 1990 — an emoji counts as 1 char locally but 2 toward Notion's limit, and a
  note was once lost to this). The condensed entry is *not* chunked: it's sent as
  the page-level `markdown` field, which Notion parses into blocks server-side —
  splitting markdown mid-token would corrupt the syntax.
- **Timezone data**: computing the Date field needs `zoneinfo` to resolve
  `LOCAL_TZ` (e.g. `Asia/Kolkata`), but Lambda's Python runtime often ships
  without the IANA timezone database. `tzdata` is in `app/requirements.txt`
  specifically so this resolves correctly instead of raising
  `ZoneInfoNotFoundError` at runtime.
- **Cloud split**: the pipeline runs on AWS (Lambda, DynamoDB) but the LLM call
  goes to Vertex AI in a separate GCP project, authenticated via Workload
  Identity Federation (see 4b — no key exists on either side). AWS stays inside
  free-tier-ish noise for a few classes a week; model usage bills through GCP
  Cloud Billing, where credits apply. The Gemini Developer API was tried first
  and abandoned: it bills against an AI Studio prepayment balance that Cloud
  credits cannot fund, so it 429'd the moment that separate balance ran dry.
- **Drive sync is a full regenerate, not an append**: `app/drive_sync.py`
  re-reads every row from Notion and re-renders the entire Doc on every run
  that finds a change. There is no per-page watermark and no diffing. This
  means an edit, deletion, or reorder in Notion is reflected correctly with no
  special-case code, at the cost of doing `O(sessions)` Notion API calls on
  every run — acceptable at the current volume (tens of sessions), revisit
  with a `last_edited_time` cache in DynamoDB if that ever grows past ~200.
- **The hash used for change detection excludes the "Last synced" timestamp
  line** (`app/drive_sync.py`, `_render_sessions` vs `_render`) — hashing the
  full rendered Doc including that line would make every run look "changed"
  even when nothing in Notion moved, since the timestamp itself always
  differs. This bit us once during development: the first version hashed the
  whole document and `changed: true` never went `false` on a second identical
  run.
- **Two separate WIF role bindings, not one**: SAM generates a distinct IAM
  execution role per Lambda function. `CondenserFunction` and
  `DriveSyncFunction` are each their own AWS role, so the WIF provider's
  `--attribute-condition` and the service account's `workloadIdentityUser`
  binding both need an entry for each role's name — widening either to a
  whole-pool or prefix match instead would let *any* future Lambda in the
  account impersonate the Vertex/Drive service account, which is a materially
  bigger trust grant than intended. See step 5 of "NotebookLM sync setup."
- **Weekend-only polling schedule**: chosen to keep the sync cheap (9
  invocations/week) at the cost of up to a multi-day lag for notes entered
  Monday–Friday directly in Notion (the Telegram bot path is unaffected — that
  pipeline is separate and instant). The Monday 00:00 IST catch-up exists
  specifically to close the gap a Sunday-evening class would otherwise leave
  until the following Saturday.
