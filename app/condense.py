import os
from pathlib import Path

from google import genai
from google.genai import types

GEMINI_MODEL_ID = os.environ["GEMINI_MODEL_ID"]

_PROMPT_TEMPLATE = (Path(__file__).parent / "prompt.txt").read_text()

# Client is built once at import (cold start), not per request.
#
# Vertex AI, not the Gemini Developer API: the Developer API bills against an AI
# Studio prepayment balance, which is a separate purse from Cloud Billing and so
# never draws on the project's Cloud credits. Vertex bills through Cloud Billing.
#
# Auth is Workload Identity Federation — there is no key. google-auth reads
# gcp-wif-credentials.json (GOOGLE_APPLICATION_CREDENTIALS), signs an AWS
# GetCallerIdentity call with the Lambda role's own credentials from the
# AWS_* env vars, exchanges that with GCP STS, then impersonates the
# teaching-notes-vertex service account. Nothing long-lived is stored anywhere.
#
# 100s request timeout sits under the Lambda's 120s ceiling, leaving room for the
# handler to catch the exception and still send its Telegram error reply. A hard
# Lambda kill would leave the user with no response at all.
_client = genai.Client(
    vertexai=True,
    project=os.environ["VERTEX_PROJECT"],
    # "global" sidesteps Vertex's per-region model availability, so there is no
    # need to check whether a given region carries this model.
    location=os.environ.get("VERTEX_LOCATION", "global"),
    http_options=types.HttpOptions(timeout=100_000),
)


def condense(raw_notes: str) -> str:
    """Call Gemini to condense raw_notes. Returns the condensed markdown entry."""
    prompt = _PROMPT_TEMPLATE.replace("{RAW_NOTES}", raw_notes)

    # temperature is deprecated and silently ignored on gemini-3.8-flash (no
    # error, no warning) — thinking_level is the real lever now. Pinned
    # explicitly to MEDIUM (this model's own default) rather than left unset,
    # so behavior doesn't silently drift if Google changes the default or
    # GEMINI_MODEL_ID moves to a model with a different one.
    response = _client.models.generate_content(
        model=GEMINI_MODEL_ID,
        contents=prompt,
        config=types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_level="MEDIUM"),
        ),
    )
    if not response.text:
        finish_reason = (
            response.candidates[0].finish_reason if response.candidates else "unknown"
        )
        raise RuntimeError(f"Model returned no text (finish_reason={finish_reason})")
    return response.text
