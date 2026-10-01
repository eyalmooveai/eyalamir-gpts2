"""Live Google Drive search for the Custom test box's general Q&A path -
"I actually want everything - build real search, not a static blob" (see
CLAUDE.md for the full decision, including why "everything" doesn't mean
reading literal every file into a prompt).

Scoped implicitly by what the Cloud Run service account itself can see
on Drive (as of this writing: the Data Science, Development, and
Product folders, shared with it directly, Viewer access) - NOT a
hardcoded folder-ID allowlist and NOT a recursive folder-tree walk.
Drive's own full-text search already only returns files the calling
identity has access to, so "the service account can read it" IS the
scope boundary here; if it's ever shared on more folders, those become
searchable the moment the sharing takes effect, with no code change -
that matches what was actually asked for, rather than a narrower slice
pinned in this file that would silently go stale.

Authenticates via Application Default Credentials, same as everything
else in this app - but Drive needs an explicit OAuth scope
(drive.readonly) that this app's BigQuery/Vertex AI calls don't request,
since Drive is a Workspace API, not a Cloud Platform system API.
"""
from __future__ import annotations

import dataclasses
import io

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
MAX_SEARCH_RESULTS = 5
MAX_SNIPPET_CHARS = 4000  # per file, keeps the Gemini prompt bounded even for a long doc

# Google-native MIME types this module gets text from via Drive's own
# export endpoint (no parsing library needed, Drive converts server
# side). Uploaded Office files need their own parser since Drive can't
# convert those the way it can its own formats - python-docx/python-pptx
# cover the two that actually showed up in a real look at these folders
# (see CLAUDE.md). PDFs aren't handled yet - a known, documented gap,
# not a silent one: add a PDF text-extraction library here if that turns
# out to matter in practice, rather than quietly returning nothing for
# every PDF forever.
_GOOGLE_NATIVE_MIME_TYPES = {
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.presentation",
    "application/vnd.google-apps.spreadsheet",
}
_DOCX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_PPTX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


class DriveNotConfigured(RuntimeError):
    """Drive ADC/scope isn't available, or the API call otherwise failed
    before any real search happened - distinguished from "found nothing
    relevant" so callers degrade to the static Moove/Archimedes
    background text instead of treating a broken search as if it had
    genuinely come up empty."""


@dataclasses.dataclass
class DriveSearchResult:
    title: str
    web_view_link: str
    snippet: str


def _drive_service():
    import google.auth
    from googleapiclient.discovery import build

    credentials, _ = google.auth.default(scopes=DRIVE_SCOPES)
    # cache_discovery=False: the client's default on-disk discovery-doc
    # cache assumes a writable, persistent filesystem - not a safe
    # assumption for a Cloud Run container.
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def _escape_query_literal(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def search_drive(question: str, max_results: int = MAX_SEARCH_RESULTS) -> list[DriveSearchResult]:
    """Full-text searches whatever Drive content the Cloud Run service
    account can currently see for `question`, returning up to
    `max_results` files with a short relevant text snippet extracted
    from each - fed into Gemini's answer alongside the static
    Moove/Archimedes background text (see custom_metrics.py's
    answer_with_drive_context()). Raises DriveNotConfigured when ADC/the
    Drive API itself isn't reachable (missing scope, no credentials, API
    not enabled, ...) - callers should treat that as "search
    unavailable" and fall back to the static background text, not "nothing
    in Drive is relevant"."""
    from google.auth.exceptions import DefaultCredentialsError, RefreshError
    from googleapiclient.errors import HttpError

    try:
        service = _drive_service()
        q = f"fullText contains '{_escape_query_literal(question)}' and trashed = false"
        response = service.files().list(
            q=q, pageSize=max_results, fields="files(id,name,mimeType,webViewLink)",
        ).execute()
    except (DefaultCredentialsError, RefreshError) as e:
        raise DriveNotConfigured("Drive credentials aren't configured here") from e
    except HttpError as e:
        raise DriveNotConfigured(f"Drive API error: {e}") from e

    results = []
    for f in response.get("files", []):
        snippet = _fetch_snippet(service, f["id"], f.get("mimeType", ""))
        results.append(DriveSearchResult(title=f["name"], web_view_link=f.get("webViewLink", ""), snippet=snippet))
    return results


def _fetch_snippet(service, file_id: str, mime_type: str) -> str:
    """Best-effort text extraction, by file type - a file this module
    can't read the content of (an unsupported type, a corrupt download,
    a transient API error, ...) just gets an empty snippet rather than
    failing the whole search; the filename/link alone is still shown to
    Gemini and the user, since the file still matched the full-text
    search even if this module can't re-extract its text itself."""
    try:
        if mime_type in _GOOGLE_NATIVE_MIME_TYPES:
            content = service.files().export(fileId=file_id, mimeType="text/plain").execute()
            text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else str(content)
            return text[:MAX_SNIPPET_CHARS]
        if mime_type == _DOCX_MIME_TYPE:
            return _extract_docx_text(_download_bytes(service, file_id))[:MAX_SNIPPET_CHARS]
        if mime_type == _PPTX_MIME_TYPE:
            return _extract_pptx_text(_download_bytes(service, file_id))[:MAX_SNIPPET_CHARS]
    except Exception:
        return ""
    return ""


def _download_bytes(service, file_id: str) -> bytes:
    from googleapiclient.http import MediaIoBaseDownload

    buf = io.BytesIO()
    request = service.files().get_media(fileId=file_id)
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def _extract_docx_text(raw: bytes) -> str:
    import docx

    document = docx.Document(io.BytesIO(raw))
    return "\n".join(p.text for p in document.paragraphs if p.text)


def _extract_pptx_text(raw: bytes) -> str:
    from pptx import Presentation

    presentation = Presentation(io.BytesIO(raw))
    bits = []
    for slide in presentation.slides:
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False) and shape.text_frame.text:
                bits.append(shape.text_frame.text)
    return "\n".join(bits)
