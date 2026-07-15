import json
import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import boto3

BEDROCK_MODEL_ID = os.environ["BEDROCK_MODEL_ID"]
LOCAL_TZ = os.environ.get("LOCAL_TZ", "Asia/Kolkata")

_bedrock = boto3.client("bedrock-runtime")
_PROMPT_TEMPLATE = (Path(__file__).parent / "prompt.txt").read_text()

_OUTPUT_CONTRACT = """

Return ONLY valid JSON, no prose, no markdown code fences, with this exact shape:
{
  "title": "<=8 words, e.g. 'Primary batch — Jul 15'",
  "students": ["names of the students who came to THIS class, exactly as spelled in the raw notes above; [] if unclear"],
  "condensed": "the full condensed journal entry as markdown, sections 1-6 exactly as specified above"
}

Rules for the "students" field:
- Only include names that actually appear in the raw notes. Never invent a name.
- Copy each name's spelling and capitalization exactly as written in the raw notes,
  so the same kid isn't recorded under two different spellings.
- This field is an attendance list, not a theme or topic tag.
"""


def _today_str() -> str:
    return datetime.now(ZoneInfo(LOCAL_TZ)).strftime("%Y-%m-%d")


def _extract_json(text: str) -> dict:
    text = text.strip()
    # Strip ```json ... ``` or ``` ... ``` fences if the model added them anyway.
    fence_match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1)
    return json.loads(text)


def condense(raw_notes: str) -> dict:
    """Call Bedrock Claude to condense raw_notes. Returns {title, students, condensed}.

    Never raises on a malformed model response — falls back to a usable result so
    a Notion row is still created with the condensed body, which is the valuable part.
    """
    prompt = _PROMPT_TEMPLATE.replace("{RAW_NOTES}", raw_notes) + _OUTPUT_CONTRACT

    response = _bedrock.converse(
        modelId=BEDROCK_MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"temperature": 0.3, "maxTokens": 4096},
    )
    model_text = response["output"]["message"]["content"][0]["text"]

    try:
        parsed = _extract_json(model_text)
        title = str(parsed.get("title") or f"Primary batch — {_today_str()}")
        students = parsed.get("students") or []
        if not isinstance(students, list):
            students = []
        students = [str(s) for s in students if str(s).strip()]
        condensed_text = str(parsed.get("condensed") or model_text)
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        title = f"Primary batch — {_today_str()}"
        students = []
        condensed_text = model_text

    return {"title": title, "students": students, "condensed": condensed_text}
