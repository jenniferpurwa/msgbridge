"""Read iMessage, SMS and RCS history from chat.db. Never writes.

Turns each source row into a plain dict the index can store: decoded text with
readable placeholders, reactions split out, edits and unsends flagged.
"""
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

import typedstream

APPLE_EPOCH = 978307200  # 2001-01-01 UTC in Unix seconds
OBJECT_REPLACEMENT = "￼"
GROUP_STYLE, DIRECT_STYLE = 43, 45

TAPBACKS = {0: "❤️", 1: "👍", 2: "👎", 3: "😂", 4: "‼️", 5: "❓", 7: "[sticker]"}
URL_RE = re.compile(r"https?://\S+")


def connect(path: Path) -> sqlite3.Connection:
    # mode=ro: SQLite refuses every write. No immutable=1, so the WAL is read too.
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def to_unix(apple_ns) -> float:
    if not apple_ns:
        return 0.0
    # Very old rows store seconds, newer rows nanoseconds.
    return (apple_ns / 1e9 if apple_ns > 1e11 else apple_ns) + APPLE_EPOCH


def from_unix(ts: float) -> int:
    return int((ts - APPLE_EPOCH) * 1e9)


def _find_nsstring(obj, depth=0):
    if depth > 8 or obj is None:
        return None
    if isinstance(obj, typedstream.types.foundation.NSString):
        return obj.value
    children = obj if isinstance(obj, (list, tuple)) else [
        getattr(obj, a) for a in ("contents", "values", "value", "elements") if hasattr(obj, a)]
    for child in children:
        found = _find_nsstring(child, depth + 1)
        if found is not None:
            return found
    return None


def _decode_bytes(blob: bytes):
    i = blob.find(b"NSString")
    if i < 0:
        return None
    rest = blob[i + len(b"NSString") + 5:]
    if not rest:
        return None
    if rest[0] == 0x81:
        n, start = int.from_bytes(rest[1:3], "little"), 3
    elif rest[0] == 0x82:
        n, start = int.from_bytes(rest[1:5], "little"), 5
    else:
        n, start = rest[0], 1
    return rest[start:start + n].decode("utf-8", errors="replace")


def decode_text(text, blob) -> str:
    if text:
        return text
    if not blob:
        return ""
    try:
        decoded = _find_nsstring(typedstream.unarchive_from_data(blob))
    except Exception:
        decoded = None
    if decoded is None:
        try:
            decoded = _decode_bytes(blob)
        except Exception:
            decoded = None
    return decoded or ""


def attachment_placeholder(mime, uti, is_sticker, is_audio_message, name) -> str | None:
    mime, uti = mime or "", uti or ""
    ext = name.rsplit(".", 1)[-1].lower() if name and "." in name else ""
    if ext == "pluginpayloadattachment":
        return None  # preview data for a link or app message
    if ext == "caf" or (is_audio_message and mime.startswith("audio/")):
        return "[voice note]"
    if ext == "pkpass":
        return "[wallet pass]"
    if is_sticker:
        return "[sticker]"
    if mime == "image/gif" or uti == "com.compuserve.gif":
        return "[GIF]"
    if mime.startswith("image/") or uti in ("public.heic", "public.jpeg", "public.png"):
        return "[photo]"
    if mime.startswith("video/") or uti.startswith("com.apple.quicktime"):
        return "[video]"
    if mime.startswith("audio/"):
        return "[audio]"
    if mime == "text/vcard" or ext == "vcf":
        return "[contact card]"
    return f"[file: {name}]" if name else "[file]"


def _link_placeholder(url: str) -> str:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return f"[link: {host}]" if host else "[link]"


def build_body(text: str, attachments: list[str], balloon: str | None) -> str:
    """Message text with attachments and links turned into short placeholders."""
    body = text.replace(OBJECT_REPLACEMENT, " ").strip()
    if balloon and "URLBalloonProvider" in balloon:
        body = URL_RE.sub(lambda m: _link_placeholder(m.group(0)), body) or "[link]"
    elif balloon and not body:
        body = "[app message]"
    parts = [body] if body else []
    parts += attachments
    return re.sub(r"[ \t]+", " ", " ".join(parts)).strip()


def strip_guid(assoc_guid: str | None) -> str | None:
    """Reaction targets look like 'p:0/GUID' or 'bp:GUID'."""
    if not assoc_guid:
        return None
    return assoc_guid.split("/", 1)[1] if "/" in assoc_guid else assoc_guid.split(":", 1)[-1]


def read_handles(con) -> dict[int, str]:
    return {r["ROWID"]: r["id"] for r in con.execute("select ROWID, id from handle")}


def read_chats(con) -> list[sqlite3.Row]:
    return con.execute("select ROWID, guid, style, display_name, group_id, original_group_id from chat").fetchall()


def chat_first_dates(con) -> dict[int, float]:
    """chat ROWID -> unix time of its first message."""
    return {r[0]: to_unix(r[1]) for r in con.execute("""
        select j.chat_id, min(m.date) from chat_message_join j join message m on m.ROWID = j.message_id
        group by j.chat_id""")}


def read_chat_handles(con) -> dict[int, list[int]]:
    out: dict[int, list[int]] = {}
    for chat_id, handle_id in con.execute("select chat_id, handle_id from chat_handle_join"):
        out.setdefault(chat_id, []).append(handle_id)
    return out


def read_messages(con, min_rowid: int = 0, since_apple: int | None = None):
    """Yield normalized messages with ROWID > min_rowid, or sent at/after since_apple."""
    where, params = ("m.ROWID > ?", [min_rowid]) if since_apple is None else ("m.date >= ?", [since_apple])
    rows = con.execute(f"""
        select m.ROWID, m.guid, m.text, m.attributedBody, m.handle_id, m.date, m.is_from_me,
               m.item_type, m.associated_message_guid, m.associated_message_type,
               m.associated_message_emoji, m.thread_originator_guid, m.date_edited,
               m.date_retracted, m.balloon_bundle_id, m.is_audio_message, m.service,
               m.group_action_type, m.other_handle,
               (select min(j.chat_id) from chat_message_join j where j.message_id = m.ROWID) as chat_id
        from message m where {where} order by m.ROWID""", params).fetchall()
    if not rows:
        return
    ids = [r["ROWID"] for r in rows]
    try:  # Messages' "Recently Deleted" folder (macOS 14+)
        deleted = {r[0] for r in con.execute("select message_id from chat_recoverable_message_join")}
    except sqlite3.OperationalError:
        deleted = set()
    atts: dict[int, list[str]] = {}
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        for r in con.execute(f"""
                select j.message_id, a.mime_type, a.uti, a.is_sticker, a.transfer_name
                from message_attachment_join j join attachment a on a.ROWID = j.attachment_id
                where j.message_id in ({",".join("?" * len(chunk))})""", chunk):
            atts.setdefault(r[0], []).append(r[1:])

    for r in rows:
        if r["ROWID"] in deleted:
            continue
        assoc = r["associated_message_type"] or 0
        base = {"rowid": r["ROWID"], "guid": r["guid"], "handle_id": r["handle_id"] or 0,
                "chat_id": r["chat_id"], "sent_at": to_unix(r["date"]), "is_from_me": bool(r["is_from_me"])}
        if 2000 <= assoc < 4000:
            kind = assoc % 1000
            emoji = r["associated_message_emoji"] if kind == 6 else TAPBACKS.get(kind)
            yield {**base, "type": "reaction", "target_guid": strip_guid(r["associated_message_guid"]),
                   "emoji": emoji or "a reaction", "removed": assoc >= 3000}
            continue
        if r["item_type"] == 1 and r["other_handle"]:
            yield {**base, "type": "member", "action": "left" if r["group_action_type"] else "joined",
                   "other_handle": r["other_handle"]}
            continue
        if r["item_type"]:
            continue  # group renames and other notices
        if r["date_retracted"]:
            body = "[unsent message]"
        else:
            placeholders = [p for a in atts.get(r["ROWID"], [])
                            if (p := attachment_placeholder(a[0], a[1], a[2], r["is_audio_message"], a[3]))]
            body = build_body(decode_text(r["text"], r["attributedBody"]), placeholders, r["balloon_bundle_id"])
        if not body:
            continue  # stored empty in the source; nothing to show
        yield {**base, "type": "message", "body": body, "edited": bool(r["date_edited"]),
               "unsent": bool(r["date_retracted"]), "reply_to_guid": r["thread_originator_guid"],
               "service": r["service"] or "iMessage"}
