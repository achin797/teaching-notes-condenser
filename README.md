# Teaching notes condenser

Send raw class notes to a Telegram bot. It condenses them with Gemini 3.6 Flash
on Vertex AI and adds a row to your Notion database: title (month and day, e.g.
"July 16"), today's date, the raw notes, and the condensed entry as the page body.

```
raw notes -> Telegram bot -> Lambda (Vertex condense + write to Notion) -> Notion row
```

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
- `GeminiModelId` — defaults to `gemini-3.6-flash`
- `LocalTz` — defaults to `Asia/Kolkata`; change if you're not in that timezone
  (used to compute the Date field correctly — Lambda runs in UTC)

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
