import os
import time

import boto3
from botocore.exceptions import ClientError

BUFFER_TABLE = os.environ["BUFFER_TABLE"]

_dynamodb = boto3.resource("dynamodb")
_table = _dynamodb.Table(BUFFER_TABLE)

# Single table holds two item kinds, distinguished by pk prefix:
#   "buf#<chat_id>" - the in-progress raw-notes buffer for a chat
#   "upd#<update_id>" - a marker for an already-processed Telegram update_id
# One table keeps infra (template.yaml) simple; a "pk" string partition key
# with no sort key is enough since each item is looked up by its full key.
BUFFER_TTL_SECONDS = 6 * 60 * 60  # abandoned buffers self-clean after 6h
DEDUP_TTL_SECONDS = 60 * 60  # dedup markers only need to outlive Telegram's retry window


def append_chunk(chat_id, text: str) -> int:
    """Append a raw-notes message to this chat's buffer. Returns the new chunk count."""
    pk = f"buf#{chat_id}"
    resp = _table.update_item(
        Key={"pk": pk},
        UpdateExpression=(
            "SET chunks = list_append(if_not_exists(chunks, :empty), :new), "
            "expireAt = :expire"
        ),
        ExpressionAttributeValues={
            ":empty": [],
            ":new": [text],
            ":expire": int(time.time()) + BUFFER_TTL_SECONDS,
        },
        ReturnValues="UPDATED_NEW",
    )
    return len(resp["Attributes"]["chunks"])


def get_and_clear_buffer(chat_id) -> list:
    """Return buffered chunks in arrival order and delete the buffer. Empty list if none."""
    pk = f"buf#{chat_id}"
    resp = _table.get_item(Key={"pk": pk})
    item = resp.get("Item")
    _table.delete_item(Key={"pk": pk})
    if not item:
        return []
    return list(item.get("chunks", []))


def is_duplicate_update(update_id) -> bool:
    """Atomically mark update_id as processed. Returns True if it was already processed."""
    pk = f"upd#{update_id}"
    try:
        _table.put_item(
            Item={"pk": pk, "expireAt": int(time.time()) + DEDUP_TTL_SECONDS},
            ConditionExpression="attribute_not_exists(pk)",
        )
        return False
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return True
        raise
