import os
from pathlib import Path

import boto3

BEDROCK_MODEL_ID = os.environ["BEDROCK_MODEL_ID"]

_bedrock = boto3.client("bedrock-runtime")
_PROMPT_TEMPLATE = (Path(__file__).parent / "prompt.txt").read_text()


def condense(raw_notes: str) -> str:
    """Call Bedrock Claude to condense raw_notes. Returns the condensed markdown entry."""
    prompt = _PROMPT_TEMPLATE.replace("{RAW_NOTES}", raw_notes)

    response = _bedrock.converse(
        modelId=BEDROCK_MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"temperature": 0.3, "maxTokens": 4096},
    )
    return response["output"]["message"]["content"][0]["text"]
