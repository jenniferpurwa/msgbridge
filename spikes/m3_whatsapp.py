"""M3 spike: what does WhatsApp for Mac store, and can we read it cleanly?

Prints counts and pass/fail only: never message text, names, phone numbers,
IDs or emoji values. Protobuf blobs are summarized by field path and type.

Usage:
    uv run spikes/m3_whatsapp.py
"""
import sqlite3
import time
import unicodedata
from collections import Counter
from pathlib import Path

GROUP = Path.home() / "Library/Group Containers/group.net.whatsapp.WhatsApp.shared"
CHAT_DB, CONTACTS_DB = GROUP / "ChatStorage.sqlite", GROUP / "ContactsV2.sqlite"
APPLE_EPOCH = 978307200

# From the column profile: media type, file extension and duration agree for these.
KNOWN = {0: "text", 1: "photo", 2: "video", 3: "voice note / audio", 4: "contact card", 5: "location",
         6: "group event", 7: "link", 8: "document", 11: "GIF", 15: "sticker"}


def connect(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def varint(buf, i):
    shift = result = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def walk(buf, prefix="", depth=0):
    """Yield (path, kind, value) for every field in a protobuf message. Raises on malformed input."""
    i = 0
    while i < len(buf):
        key, i = varint(buf, i)
        field, wire = key >> 3, key & 7
        path = f"{prefix}.{field}" if prefix else str(field)
        if wire == 0:
            v, i = varint(buf, i)
            yield path, "int", v
        elif wire == 1:
            yield path, "fixed64", buf[i:i + 8]
            i += 8
        elif wire == 5:
            yield path, "fixed32", buf[i:i + 4]
            i += 4
        elif wire == 2:
            n, i = varint(buf, i)
            chunk = buf[i:i + n]
            i += n
            nested = None
            if depth < 6 and chunk:
                try:
                    nested = list(walk(chunk, path, depth + 1))
                except Exception:
                    nested = None
            if nested:
                yield path, "message", chunk
                yield from nested
            else:
                try:
                    yield path, "string", chunk.decode("utf-8")
                except UnicodeDecodeError:
                    yield path, "bytes", chunk
        else:
            raise ValueError("unsupported wire type")


def is_emoji(s: str) -> bool:
    s = s.replace("️", "").replace("‍", "")
    return 0 < len(s) <= 8 and all(unicodedata.category(ch) in ("So", "Sk", "Mn") or 0x1F3FB <= ord(ch) <= 0x1F3FF
                                   for ch in s)


def main():
    con = connect(CHAT_DB)
    results = {}
    print("== Setup")
    try:
        con.execute("create table msgbridge_write_test(x)")
        results["read-only: writes are refused"] = False
    except sqlite3.OperationalError:
        results["read-only: writes are refused"] = True

    total = con.execute("select count(*) from ZWAMESSAGE").fetchone()[0]
    first, last = con.execute("select min(ZMESSAGEDATE), max(ZMESSAGEDATE) from ZWAMESSAGE where ZMESSAGEDATE > 0").fetchone()
    day = lambda d: time.strftime("%Y-%m-%d", time.localtime(d + APPLE_EPOCH))
    print(f"Messages: {total:,} ({day(first)} to {day(last)})")
    per_year = con.execute("""select strftime('%Y', ZMESSAGEDATE + 978307200, 'unixepoch'), count(*) from ZWAMESSAGE
                              where ZMESSAGEDATE > 0 group by 1""").fetchall()
    print("By year: " + ", ".join(f"{y} {n:,}" for y, n in per_year))
    kinds = dict(con.execute("select ZSESSIONTYPE, count(*) from ZWACHATSESSION group by 1").fetchall())
    print(f"Chats: 1:1 {kinds.get(0, 0)}, groups {kinds.get(1, 0) + kinds.get(4, 0)}, "
          f"status/broadcast {kinds.get(2, 0) + kinds.get(3, 0)}")

    print("\n== Message types")
    types = con.execute("select ZMESSAGETYPE, count(*) from ZWAMESSAGE group by 1 order by 2 desc").fetchall()
    known = sum(n for t, n in types if t in KNOWN)
    print("Known: " + ", ".join(f"{KNOWN[t]} {n:,}" for t, n in types if t in KNOWN))
    print("Unmapped: " + ", ".join(f"type {t} {n:,}" for t, n in types if t not in KNOWN))
    results["types: >= 97% of messages have a known type"] = known / total >= 0.97
    print(f"Known share: {100 * known / total:.1f}%")

    print("\n== Identity")
    lid_phone = {}
    if CONTACTS_DB.exists():
        c2 = connect(CONTACTS_DB)
        for lid, wid in c2.execute("select ZLID, ZWHATSAPPID from ZWAADDRESSBOOKCONTACT where ZLID is not null and ZWHATSAPPID is not null"):
            lid_phone[lid] = wid
    lids = {r[0] for r in con.execute("""select ZCONTACTJID from ZWACHATSESSION where ZCONTACTJID like '%@lid'
                                         union select ZMEMBERJID from ZWAGROUPMEMBER where ZMEMBERJID like '%@lid'""")}
    mapped = sum(1 for lid in lids if lid in lid_phone)
    print(f"Privacy IDs (@lid) seen: {len(lids):,}; mapped to a phone number via ContactsV2: {mapped:,}")
    named = con.execute("""select count(*), sum(ZPARTNERNAME is not null and ZPARTNERNAME != '')
                           from ZWACHATSESSION where ZSESSIONTYPE = 0""").fetchone()
    print(f"1:1 chats with a display name: {named[1]:,} / {named[0]:,}")
    unknown_senders = con.execute("""select count(*) from ZWAMESSAGE m join ZWACHATSESSION s on s.Z_PK = m.ZCHATSESSION
        where s.ZSESSIONTYPE in (1, 4) and m.ZISFROMME = 0 and m.ZGROUPMEMBER is null
          and (m.ZPUSHNAME is null or m.ZPUSHNAME = '') and m.ZMESSAGETYPE not in (6, 10)""").fetchone()[0]
    print(f"Group messages with no identifiable sender: {unknown_senders:,}")

    print("\n== Reactions (ZWAMESSAGEINFO.ZRECEIPTINFO)")
    paths, emoji_paths, bad = Counter(), Counter(), 0
    for (blob,) in con.execute("select ZRECEIPTINFO from ZWAMESSAGEINFO where ZRECEIPTINFO is not null"):
        try:
            for path, kind, value in walk(blob):
                paths[(path, kind)] += 1
                if kind == "string" and is_emoji(value):
                    emoji_paths[path] += 1
        except Exception:
            bad += 1
    print(f"Blobs that failed to parse: {bad:,}")
    print("Fields holding emoji: " + (", ".join(f"{p} x{n:,}" for p, n in emoji_paths.most_common(5)) or "none"))
    if emoji_paths:
        top = emoji_paths.most_common(1)[0][0]
        parent = top.rsplit(".", 1)[0]
        siblings = sorted({(p, k) for (p, k) in paths if p.startswith(parent + ".")})
        print(f"Structure next to {top}: " + ", ".join(f"{p}:{k}" for p, k in siblings))
    results["reactions: emoji found in receipt info"] = bool(emoji_paths)

    print("\n== Replies (quoted message id inside ZWAMEDIAITEM.ZMETADATA)")
    stanzas = {r[0] for r in con.execute("select ZSTANZAID from ZWAMESSAGE where ZSTANZAID is not null")}
    reply_paths, with_meta = Counter(), 0
    for (blob,) in con.execute("""select i.ZMETADATA from ZWAMESSAGE m join ZWAMEDIAITEM i on i.Z_PK = m.ZMEDIAITEM
                                  where i.ZMETADATA is not null"""):
        with_meta += 1
        try:
            for path, kind, value in walk(blob):
                if kind == "string" and value in stanzas:
                    reply_paths[path] += 1
        except Exception:
            pass
    linked = sum(reply_paths.values())
    print(f"Metadata blobs: {with_meta:,}; quoting a known message: {linked:,}; at " +
          (", ".join(f"{p} x{n:,}" for p, n in reply_paths.most_common(3)) or "none"))
    results["replies: quoted message ids resolve"] = linked > 0

    print("\n== Result")
    for name, ok in results.items():
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    print("M3 spike " + ("PASSED" if all(results.values()) else "FAILED"))


if __name__ == "__main__":
    main()
