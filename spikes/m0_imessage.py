"""M0 spike: can we read iMessage history from chat.db and decode its text?

Prints counts and pass/fail only. It never prints message text, phone
numbers, emails or contact names, so its output is safe to share.

Usage:
    uv run spikes/m0_imessage.py            # last 1,000 messages
    uv run spikes/m0_imessage.py --limit 0  # whole history
"""
import argparse
import sqlite3
import time
from collections import Counter
from pathlib import Path

import typedstream

DB_PATH = Path.home() / "Library/Messages/chat.db"
APPLE_EPOCH = 978307200  # 2001-01-01 UTC in Unix seconds
OBJECT_REPLACEMENT = "￼"  # inline marker where an attachment sits in the text

EXPECTED_COLUMNS = {
    "message": [
        "ROWID", "guid", "text", "attributedBody", "handle_id", "date", "is_from_me",
        "item_type", "associated_message_guid", "associated_message_type",
        "associated_message_emoji", "thread_originator_guid", "date_edited",
        "date_retracted", "message_summary_info", "balloon_bundle_id", "payload_data",
        "cache_has_attachments", "is_audio_message",
    ],
    "chat": ["ROWID", "guid", "style", "chat_identifier", "display_name", "service_name"],
    "handle": ["ROWID", "id", "service"],
    "attachment": ["ROWID", "mime_type", "uti", "transfer_name", "total_bytes", "is_sticker", "filename"],
    "chat_message_join": ["chat_id", "message_id"],
    "chat_handle_join": ["chat_id", "handle_id"],
    "message_attachment_join": ["message_id", "attachment_id"],
}

GROUP_STYLE, DIRECT_STYLE = 43, 45


def connect():
    # mode=ro: SQLite refuses every write. No immutable=1, so the WAL is read too.
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


def check_schema(con):
    missing = []
    for table, cols in EXPECTED_COLUMNS.items():
        have = {r[1] for r in con.execute(f"pragma table_info({table})")}
        missing += [f"{table}.{c}" for c in cols if c not in have]
    return missing


def check_read_only(con):
    try:
        con.execute("create table msgbridge_write_test(x)")
    except sqlite3.OperationalError:
        return True
    return False


def _find_nsstring(obj, depth=0):
    """Walk an unarchived NSAttributedString and return its first NSString value."""
    if depth > 8 or obj is None:
        return None
    if isinstance(obj, typedstream.types.foundation.NSString):
        return obj.value
    if isinstance(obj, (list, tuple)):
        children = obj
    else:
        children = [getattr(obj, a) for a in ("contents", "values", "value", "elements") if hasattr(obj, a)]
    for child in children:
        found = _find_nsstring(child, depth + 1)
        if found is not None:
            return found
    return None


def decode_library(blob):
    return _find_nsstring(typedstream.unarchive_from_data(blob))


def decode_bytes(blob):
    """Fallback: read the length-prefixed string after the NSString class name."""
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
    return rest[start:start + n].decode("utf-8", errors="strict")


def classify_attachment(mime, uti, is_sticker, is_audio_message, name):
    mime, uti = mime or "", uti or ""
    ext = name.rsplit(".", 1)[-1].lower() if name and "." in name else ""
    if ext == "pluginpayloadattachment":
        return None  # preview data for a link or app message, already counted via balloon_bundle_id
    if ext == "caf":
        return "voice note"
    if ext == "pkpass":
        return "wallet pass"
    if is_sticker:
        return "sticker"
    if mime == "image/gif" or uti == "com.compuserve.gif":
        return "gif"
    if mime.startswith("image/") or uti.startswith("public.heic") or uti == "public.jpeg":
        return "photo"
    if mime.startswith("video/") or uti.startswith("com.apple.quicktime"):
        return "video"
    if mime.startswith("audio/") or uti == "com.apple.coreaudio-format":
        return "voice note" if is_audio_message else "audio"
    if not mime and not uti:
        return "unknown (not downloaded?)"
    return "file"


def fmt_pct(n, d):
    return f"{n:,} / {d:,} ({100 * n / d:.2f}%)" if d else f"{n:,} / 0"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=1000, help="newest N messages to decode; 0 = all")
    args = ap.parse_args()

    con = connect()
    results = {}

    print("== Setup")
    missing = check_schema(con)
    results["schema: all expected columns present"] = not missing
    print(f"Missing columns: {', '.join(missing) if missing else 'none'}")
    results["read-only: writes are refused"] = check_read_only(con)

    total = con.execute("select count(*) from message").fetchone()[0]
    first, last = con.execute("select min(date), max(date) from message where date > 0").fetchone()
    to_day = lambda d: time.strftime("%Y-%m-%d", time.localtime(d / 1e9 + APPLE_EPOCH))
    print(f"Messages in history: {total:,} ({to_day(first)} to {to_day(last)})")

    chats = Counter()
    for style, name in con.execute("select style, display_name from chat"):
        if style == GROUP_STYLE:
            chats["group, named" if name else "group, unnamed"] += 1
        elif style == DIRECT_STYLE:
            chats["1:1"] += 1
        else:
            chats[f"other style {style}"] += 1
    print("Threads: " + ", ".join(f"{k} {v:,}" for k, v in sorted(chats.items())))
    services = Counter(r[0] for r in con.execute("select coalesce(service, 'unknown') from message"))
    print("Messages by service: " + ", ".join(f"{k} {v:,}" for k, v in services.most_common()))

    limit_sql = f"limit {args.limit}" if args.limit else ""
    rows = con.execute(f"""
        select ROWID, text, attributedBody, item_type, associated_message_type,
               thread_originator_guid, date_edited, date_retracted, balloon_bundle_id,
               cache_has_attachments, is_audio_message
        from message order by date desc {limit_sql}""").fetchall()

    att = {}
    for mid, mime, uti, sticker, name in con.execute(f"""
        select j.message_id, a.mime_type, a.uti, a.is_sticker, a.transfer_name
        from message_attachment_join j join attachment a on a.ROWID = j.attachment_id
        where j.message_id in (select ROWID from message order by date desc {limit_sql})"""):
        att.setdefault(mid, []).append((mime, uti, sticker, name))
    in_chat = {r[0] for r in con.execute("select distinct message_id from chat_message_join")}

    print(f"\n== Sample: newest {len(rows):,} messages")
    kinds, placeholders, errors = Counter(), Counter(), Counter()
    content_total = content_with_text = 0
    lib_ok = byte_ok = both_present = agree = blob_rows = lib_fail_byte_ok = 0
    started = time.perf_counter()

    for (rid, text, blob, item_type, assoc_type, reply_guid, edited, retracted,
         balloon, has_att, is_audio) in rows:
        decoded = None
        if blob:
            blob_rows += 1
            try:
                decoded = decode_library(blob)
            except Exception as e:  # count only; never print content
                errors[f"library: {type(e).__name__}"] += 1
            if decoded is not None:
                lib_ok += 1
            try:
                via_bytes = decode_bytes(blob)
            except Exception as e:
                via_bytes = None
                errors[f"bytes: {type(e).__name__}"] += 1
            if via_bytes is not None:
                byte_ok += 1
                if decoded is None:
                    lib_fail_byte_ok += 1
                    decoded = via_bytes
            if text:
                both_present += 1
                agree += decoded == text

        body = (text or decoded or "").replace(OBJECT_REPLACEMENT, "").strip()

        if 2000 <= (assoc_type or 0) < 3000:
            kinds["reaction added"] += 1
            continue
        if 3000 <= (assoc_type or 0) < 4000:
            kinds["reaction removed"] += 1
            continue
        if item_type:
            kinds["system event (rename, join, leave)"] += 1
            continue
        if retracted:
            kinds["unsent"] += 1
            continue
        if rid not in in_chat:
            kinds["not in any thread (skipped)"] += 1
            continue

        if edited:
            kinds["edited"] += 1
        if reply_guid:
            kinds["inline reply"] += 1
        for mime, uti, sticker, name in att.get(rid, []):
            label = classify_attachment(mime, uti, sticker, is_audio, name)
            if label:
                placeholders[label] += 1
        if balloon:
            placeholders["link preview" if "URLBalloonProvider" in balloon else "app / extension message"] += 1

        if att.get(rid) or balloon:
            if not body:
                kinds["attachment or app only, no text"] += 1
                continue
        if not body and (text or decoded or "") == "" and blob is not None:
            # Decoded fine, but the source stores an empty string: nothing to recover.
            kinds["empty in source"] += 1
            continue
        content_total += 1
        content_with_text += bool(body)

    elapsed = time.perf_counter() - started
    print(f"Rows with attributedBody: {blob_rows:,}; rows with only plain text: "
          f"{sum(1 for r in rows if r[1] and not r[2]):,}")
    print(f"Decoded by pytypedstream: {fmt_pct(lib_ok, blob_rows)}")
    print(f"Decoded by byte fallback: {fmt_pct(byte_ok, blob_rows)}")
    print(f"Rescued by fallback when library failed: {lib_fail_byte_ok:,}")
    print(f"Decoded matches plain text (where both exist): {fmt_pct(agree, both_present)}")
    print(f"Text messages with readable text: {fmt_pct(content_with_text, content_total)}")
    print("Message kinds: " + (", ".join(f"{k} {v:,}" for k, v in kinds.most_common()) or "none"))
    print("Placeholders needed: " + (", ".join(f"{k} {v:,}" for k, v in placeholders.most_common()) or "none"))
    print("Decode errors: " + (", ".join(f"{k} {v:,}" for k, v in errors.most_common()) or "none"))
    print(f"Decode time: {elapsed:.2f}s ({1000 * elapsed / max(len(rows), 1):.3f} ms per message)")

    results["decode: >= 99% of text messages readable"] = (
        content_total > 0 and content_with_text / content_total >= 0.99)
    results["decode: >= 99% agree with plain text"] = (
        both_present == 0 or agree / both_present >= 0.99)

    print("\n== Result")
    for name, ok in results.items():
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    print("M0 " + ("PASSED" if all(results.values()) else "FAILED"))


if __name__ == "__main__":
    main()
