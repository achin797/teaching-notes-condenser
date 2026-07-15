# Teaching notes condenser

Send raw class notes to a Telegram bot. It condenses them with Bedrock Claude
into the journal-entry format below and adds a row to your Notion database with
the raw notes, the condensed entry, a title (the month and day, e.g. "July 16"),
and today's date.

```
raw notes -> Telegram bot -> Lambda (Bedrock condense + write to Notion) -> Notion row
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
      ├─ condenses the notes on /done ─────────────────▶ Bedrock (Claude)
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
| Bedrock (Claude)  | runs the condensing prompt over the raw notes                     | `app/condense.py`, `app/prompt.txt` |
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
4. **AWS account, CLI configured, and Bedrock model access enabled** (see below
   — this is the part most first-time AWS users get stuck on).
5. **AWS SAM CLI** installed: `brew install awscli aws-sam-cli`.

### 4a. Set up AWS CLI credentials (first time only, per AWS account)

Don't use your root account login for the CLI. Instead:
- AWS Console → IAM → Users → Create user → attach `AdministratorAccess`
  (fine for a personal project; tighten later if you want).
- That user → Security credentials → Create access key → choose
  "Command Line Interface (CLI)" → copy the Access Key ID + Secret Access Key
  (shown only once).
- Run `aws configure` locally and paste them in, along with a default region
  (pick one where Bedrock + Claude are available, e.g. `us-east-1` or
  `us-west-1`) and output format `json`.

### 4b. Enable Bedrock model access + get the right model ID

Bedrock models are off by default per account, even in your own account:
- Console → Bedrock → **Model access** (left sidebar) → Manage model access →
  enable the Claude model you want → Submit. Usually near-instant for
  Anthropic models, but confirm it shows "Access granted" before deploying.
- **Newer Claude models require an inference profile ID, not the bare model
  ID.** If you deploy with a plain model ID like `anthropic.claude-sonnet-4-6`
  you'll get:
  ```
  ValidationException: Invocation of model ID anthropic.claude-sonnet-4-6 with
  on-demand throughput isn't supported. Retry your request with the ID or ARN
  of an inference profile that contains this model.
  ```
  Find the correct profile ID with:
  ```bash
  aws bedrock list-inference-profiles \
    --query "inferenceProfileSummaries[].inferenceProfileId" --output text
  ```
  It'll look like `global.anthropic.claude-sonnet-4-6` (routes worldwide to
  wherever has capacity) or a region-prefixed variant like
  `us.anthropic.claude-sonnet-4-6`. Use that full string as your
  `BedrockModelId` parameter below — not the bare model ID.

## Confirm the Notion database (before deploying)

```bash
export NOTION_TOKEN=ntn_...
curl -s https://api.notion.com/v1/databases/39a3ef887ec880fea091f615459e13f5 \
  -H "Authorization: Bearer $NOTION_TOKEN" \
  -H "Notion-Version: 2022-06-28" | python3 -m json.tool
```

Expect `properties` to contain `Name`, `Condensed notes`, `Raw notes`, `Date`.
If you get a 404, redo the "share with integration" step.

## Deploy

```bash
sam build
sam deploy --guided
```

You'll be prompted for the stack parameters:
- `TelegramBotToken`
- `TelegramWebhookSecret` — make up a random string (e.g. `openssl rand -hex 20`)
- `AllowedChatId` — your Telegram user id
- `NotionToken`
- `NotionDbId` — defaults to the database already wired up in this repo
- `BedrockModelId` — the **inference profile ID** from step 4b above (e.g.
  `global.anthropic.claude-sonnet-4-6`), not the bare model ID. Recommend a
  Sonnet-class model; Opus is ~5-10x pricier for no quality gain on notes this
  short.
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

Other commands: `/start` or `/help` for usage instructions.

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

**Bedrock `ValidationException` about on-demand throughput** — see step 4b
under Prerequisites; you need the inference profile ID, not the bare model ID.

**The bot replies with a generic error** — get the real traceback:
```bash
sam logs --stack-name <your-stack-name> --tail
```
(find `<your-stack-name>` with `grep stack_name samconfig.toml`) then trigger
the bot again while it's tailing.

## Verification checklist

- [ ] Notion access curl above returns the 4 expected properties.
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
  plain-text messages are buffered per chat in DynamoDB until you send `/done`.
  Buffers auto-expire after 6 hours if abandoned.
- **Idempotency**: Telegram retries the webhook if it doesn't get a fast 200.
  Each `update_id` is recorded in DynamoDB (1h TTL) so a slow Bedrock call never
  creates a duplicate Notion row.
- **Chunking**: Notion caps a single rich_text object at 2000 characters. Long
  raw notes and condensed entries are split into multiple rich_text chunks in
  the database properties, and mirrored as paragraph blocks in the page body
  (under "Condensed" / "Raw notes" headings) for comfortable reading.
- **Timezone data**: computing the Date field needs `zoneinfo` to resolve
  `LOCAL_TZ` (e.g. `Asia/Kolkata`), but Lambda's Python runtime often ships
  without the IANA timezone database. `tzdata` is in `app/requirements.txt`
  specifically so this resolves correctly instead of raising
  `ZoneInfoNotFoundError` at runtime.
