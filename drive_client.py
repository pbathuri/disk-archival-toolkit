"""Google Drive API v3: OAuth desktop flow, folder creation, file upload."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Callable, Optional, TypeVar

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

T = TypeVar("T")

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def _retry_drive_call(fn: Callable[[], T], max_attempts: int = 5) -> T:
    """Call fn() with exponential backoff on retryable Drive API errors."""
    for attempt in range(max_attempts):
        try:
            return fn()
        except HttpError as e:
            if e.resp.status not in _RETRYABLE_STATUS or attempt == max_attempts - 1:
                raise
            wait = 2 ** attempt
            print(f"  Drive API {e.resp.status}, retry {attempt + 1}/{max_attempts - 1} in {wait}s…", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError("unreachable")

SCOPES = ["https://www.googleapis.com/auth/drive"]

_TOKEN_FILENAME = "token.json"


def load_dotenv_project() -> None:
    env = Path(__file__).resolve().parent / ".env"
    if env.is_file():
        load_dotenv(env)


def _client_config_from_env() -> dict:
    load_dotenv_project()
    cid = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    if not cid or not secret:
        raise SystemExit(
            "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env (or use GOOGLE_OAUTH_CLIENT_SECRETS_JSON)."
        )
    return {
        "installed": {
            "client_id": cid,
            "client_secret": secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://127.0.0.1", "http://localhost"],
        }
    }


def _client_config_from_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def get_credentials(project_dir: Path) -> Credentials:
    load_dotenv_project()
    token_path = project_dir / _TOKEN_FILENAME
    creds: Optional[Credentials] = None
    if token_path.is_file():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
            return creds
        except Exception:
            creds = None

    json_path = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRETS_JSON", "").strip()
    if json_path:
        cfg = _client_config_from_json(Path(json_path).expanduser())
    else:
        cfg = _client_config_from_env()

    flow = InstalledAppFlow.from_client_config(cfg, SCOPES)
    print(
        "\nOAuth: complete sign-in in your browser when it opens.\n"
        "If nothing opens, copy the https://accounts.google.com/... URL from the next line.\n"
        "Do not press Ctrl+C until you see the localhost redirect finish (then uploads start).\n",
        file=sys.stderr,
    )
    creds = flow.run_local_server(port=0, open_browser=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    print(
        f"Saved refresh token to {token_path} — future runs skip the browser until it expires.\n", file=sys.stderr)
    return creds


def build_service(project_dir: Path):
    creds = get_credentials(project_dir)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


_SAFE_NAME = re.compile(r'[\x00-\x1f\\/:*?"<>|]+')


def safe_drive_name(name: str, max_len: int = 200) -> str:
    s = _SAFE_NAME.sub("_", name.strip()) or "_"
    return s[:max_len]


def _escape_drive_query_literal(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def get_or_create_folder(service, name: str, parent_id: Optional[str]) -> str:
    name = safe_drive_name(name)
    esc = _escape_drive_query_literal(name)
    q = f"name = '{esc}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    if parent_id:
        q += f" and '{parent_id}' in parents"
    else:
        q += " and 'root' in parents"
    r = _retry_drive_call(
        lambda: service.files()
        .list(q=q, spaces="drive", fields="files(id, name)", pageSize=10)
        .execute()
    )
    files = r.get("files", [])
    if files:
        return files[0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        meta["parents"] = [parent_id]
    folder = _retry_drive_call(
        lambda: service.files().create(body=meta, fields="id").execute()
    )
    return folder["id"]


def ensure_folder_path(service, segments: list[str], root_parent_id: Optional[str]) -> str:
    parent = root_parent_id
    for seg in segments:
        if not seg:
            continue
        parent = get_or_create_folder(service, seg, parent)
    return parent


def upload_local_file(
    service,
    local_path: Path,
    parent_folder_id: str,
    drive_file_name: Optional[str] = None,
    description: Optional[str] = None,
) -> str:
    local_path = local_path.expanduser().resolve()
    if not local_path.is_file():
        raise FileNotFoundError(local_path)

    name = safe_drive_name(drive_file_name or local_path.name)
    size = local_path.stat().st_size
    body: dict = {"name": name, "parents": [parent_folder_id]}
    if description:
        body["description"] = description[:12000]

    use_resumable = size > 5 * 1024 * 1024
    if use_resumable:
        media = MediaFileUpload(str(local_path), resumable=True, chunksize=10 * 1024 * 1024)
        request = service.files().create(body=body, media_body=media, fields="id")
        response = None
        while response is None:
            status, response = _retry_drive_call(request.next_chunk)
            if status:
                pct = status.progress() * 100
                print(f"\r  uploading {name}: {pct:.1f}%", end="", file=sys.stderr)
        print(file=sys.stderr)  # newline after progress
    else:
        media = MediaFileUpload(str(local_path), resumable=False)
        request = service.files().create(body=body, media_body=media, fields="id")
        response = _retry_drive_call(request.execute)

    if not response or "id" not in response:
        raise RuntimeError("Drive upload finished without file id")
    return response["id"]
