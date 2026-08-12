import os

import google.auth
import requests
from google.auth.transport.requests import Request

DRIVE_DOC_ID = os.environ["DRIVE_DOC_ID"]

# Full drive scope, not the narrower drive.file: drive.file only covers files
# the calling app itself created, but this Doc is created and owned by the
# human user and merely shared with the service account. The SA's Drive
# access is bounded by that one share regardless of which scope is requested,
# so drive.file would just 404 on a file it didn't create.
_SCOPES = ["https://www.googleapis.com/auth/drive"]
_DOC_MIME = "application/vnd.google-apps.document"
# Set from the Step 1 smoke-test outcome. Fallback ladder if this rung fails
# in production: "text/html" (render markdown -> minimal HTML first), then
# "text/plain" as a last resort (content is still searchable, just unstyled).
_MEDIA_MIME = "text/markdown"
_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files/{file_id}?uploadType=multipart"

# Credentials are built once at import (cold start), refreshed per invocation
# below rather than cached, since a Lambda container can be reused across
# invocations and a cached token can expire mid-lifetime.
#
# app/gcp-wif-credentials.json (GOOGLE_APPLICATION_CREDENTIALS) is an
# external_account credential with no hardcoded scopes, so the scope
# requested here flows through to the impersonation call for
# teaching-notes-vertex@ — same file, same service account condense.py
# uses for Vertex, only the requested scope differs.
_credentials, _ = google.auth.default(scopes=_SCOPES)


def _token() -> str:
    _credentials.refresh(Request())
    return _credentials.token


def replace_doc(markdown: str) -> None:
    """Overwrite the target Google Doc's full content from markdown.

    Drive's media-upload conversion replaces the entire document body on
    update — there is no append or patch-in-place. That is deliberate here:
    the caller always sends a freshly regenerated full document, so a full
    overwrite is exactly the idempotent behavior wanted (see drive_sync.py).
    """
    boundary = "notion_drive_sync_boundary"
    body = (
        f"--{boundary}\r\n"
        "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f'{{"mimeType": "{_DOC_MIME}"}}\r\n'
        f"--{boundary}\r\n"
        f"Content-Type: {_MEDIA_MIME}; charset=UTF-8\r\n\r\n"
        f"{markdown}\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")

    # Built by hand rather than requests' files=: that produces
    # multipart/form-data, which Drive's upload endpoint rejects. Drive
    # requires multipart/related with this exact two-part shape.
    resp = requests.patch(
        _UPLOAD_URL.format(file_id=DRIVE_DOC_ID),
        headers={
            "Authorization": f"Bearer {_token()}",
            "Content-Type": f"multipart/related; boundary={boundary}",
        },
        data=body,
        timeout=60,
    )
    if not resp.ok:
        print(f"Drive API error {resp.status_code}: {resp.text}")
    resp.raise_for_status()
