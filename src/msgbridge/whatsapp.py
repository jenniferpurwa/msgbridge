"""Read WhatsApp for Mac history from ChatStorage.sqlite. Never writes.

WhatsApp stores reactions and reply links in protobuf blobs; the field numbers
below come from the M3 spike (spikes/m3_whatsapp.py), which checked them
against the real database without printing any content.
"""
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

from .imessage import APPLE_EPOCH, URL_RE

DEFAULT_DIR = Path.home() / "Library/Group Containers/group.net.whatsapp.WhatsApp.shared"

DIRECT, GROUP, COMMUNITY = 0, 1, 4
SYSTEM_TYPES = {6, 10}          # group events and chat notices
REPLY_FIELD = 5                 # ZWAMEDIAITEM.ZMETADATA: quoted message stanza id
REACTIONS_FIELD, REACTION = 7, 1  # ZWAMESSAGEINFO.ZRECEIPTINFO: repeated reaction records
REACTOR_FIELD, EMOJI_FIELD, TIME_FIELD = 2, 3, 4


def connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def to_unix(apple_seconds) -> float:
    return (apple_seconds or 0) + APPLE_EPOCH


def from_unix(ts: float) -> float:
    return ts - APPLE_EPOCH


# -- protobuf (wire format only; no schema needed) -------------------------------
def _varint(buf: bytes, i: int):
    shift = result = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def fields(buf: bytes):
    """Yield (field_number, value) at one level: ints for varints, bytes for length-delimited."""
    i = 0
    while i < len(buf):
        key, i = _varint(buf, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _varint(buf, i)
        elif wire == 2:
            n, i = _varint(buf, i)
            value, i = buf[i:i + n], i + n
        elif wire == 1:
            value, i = buf[i:i + 8], i + 8
        elif wire == 5:
            value, i = buf[i:i + 4], i + 4
        else:
            return
        yield field, value


def quoted_stanza(metadata: bytes | None) -> str | None:
    if not metadata:
        return None
    try:
        for f, v in fields(metadata):
            if f == REPLY_FIELD and isinstance(v, bytes):
                return v.decode("utf-8")
    except Exception:
        return None
    return None


def reactions(receipt: bytes | None) -> list[tuple[str, str, float]]:
    """[(reactor_jid, emoji, unix_time)] currently on a message; removed reactions have no emoji.

    Your own reactions carry no reactor id, so they come back with reactor_jid "" (= you).
    """
    out = []
    if not receipt:
        return out
    try:
        for f, container in fields(receipt):
            if f != REACTIONS_FIELD or not isinstance(container, bytes):
                continue
            for f2, record in fields(container):
                if f2 != REACTION or not isinstance(record, bytes):
                    continue
                rec = dict(fields(record))
                emoji = rec.get(EMOJI_FIELD, b"").decode("utf-8", "replace")
                jid = rec.get(REACTOR_FIELD, b"").decode("utf-8", "replace")
                ts = rec.get(TIME_FIELD, 0)
                if emoji:
                    out.append((jid, emoji, ts / 1000 if ts else 0.0))
    except Exception:
        return out
    return out


# -- bodies ----------------------------------------------------------------------
def _duration(seconds) -> str:
    s = int(seconds or 0)
    return f" {s // 60}:{s % 60:02d}" if s else ""


def _host(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def build_body(mtype: int, text: str | None, title: str | None, duration, media_url_text: str | None = None) -> str | None:
    """Readable text for one message, or None for events that are not messages."""
    text = (text or "").strip()
    title = (title or "").strip()
    if mtype in SYSTEM_TYPES:
        return None
    if mtype == 0:
        return text or None
    if mtype == 1:
        return f"[photo] {title}".strip()
    if mtype == 2:
        return f"[video{_duration(duration)}] {title}".strip()
    if mtype == 3:
        return f"[voice note{_duration(duration)}]"
    if mtype == 4:
        return "[contact card]"
    if mtype == 5:
        return "[location]"
    if mtype == 7:
        urls = URL_RE.findall(text)
        host = _host(urls[0]) if urls else ""
        label = f"[link: {host}" + (f" – {title}" if title else "") + "]" if host else "[link]"
        rest = URL_RE.sub("", text).strip()
        return f"{rest} {label}".strip()
    if mtype == 8:
        return "[document]" + (f" {title}" if title else "")
    if mtype == 11:
        return "[GIF]"
    if mtype == 15:
        return "[sticker]"
    # Undocumented types: keep any text, otherwise a neutral placeholder.
    return text or "[WhatsApp item]"


def read_lid_phones(contacts_db: Path) -> dict[str, str]:
    """Map privacy IDs (...@lid) to phone JIDs using WhatsApp's own contact store."""
    if not contacts_db.exists():
        return {}
    con = connect(contacts_db)
    try:
        return {r[0]: r[1] for r in con.execute(
            "select ZLID, ZWHATSAPPID from ZWAADDRESSBOOKCONTACT where ZLID is not null and ZWHATSAPPID is not null")}
    finally:
        con.close()


def jid_phone(jid: str, lid_phones: dict[str, str]) -> str | None:
    """Phone number in +digits form for a person JID, if one is known."""
    if not jid:
        return None
    if jid.endswith("@lid"):
        jid = lid_phones.get(jid, "")
    if jid.endswith("@s.whatsapp.net"):
        return "+" + re.sub(r"\D", "", jid.split("@")[0])
    return None


def read_chats(con):
    return con.execute("""select Z_PK, ZSESSIONTYPE, ZCONTACTJID, ZPARTNERNAME from ZWACHATSESSION
                          where ZSESSIONTYPE in (0, 1, 4) and coalesce(ZREMOVED, 0) = 0""").fetchall()


def read_members(con) -> dict[int, list[tuple[str, str]]]:
    """chat Z_PK -> [(member_jid, best display name)]"""
    out: dict[int, list[tuple[str, str]]] = {}
    for chat, jid, name, first in con.execute(
            "select ZCHATSESSION, ZMEMBERJID, ZCONTACTNAME, ZFIRSTNAME from ZWAGROUPMEMBER where ZMEMBERJID is not null"):
        out.setdefault(chat, []).append((jid, name or first or ""))
    return out


def read_push_names(con) -> dict[str, str]:
    return {r[0]: r[1] for r in con.execute("select ZJID, ZPUSHNAME from ZWAPROFILEPUSHNAME where ZPUSHNAME is not null")}


def read_messages(con, min_pk: int = 0, since_apple: float | None = None):
    where, params = ("m.Z_PK > ?", [min_pk]) if since_apple is None else ("m.ZMESSAGEDATE >= ?", [since_apple])
    for r in con.execute(f"""
            select m.Z_PK, m.ZSTANZAID, m.ZCHATSESSION, m.ZISFROMME, m.ZMESSAGEDATE, m.ZMESSAGETYPE, m.ZTEXT,
                   m.ZFROMJID, m.ZPUSHNAME, g.ZMEMBERJID as member_jid,
                   i.ZTITLE, i.ZMOVIEDURATION, i.ZMETADATA, info.ZRECEIPTINFO
            from ZWAMESSAGE m
            left join ZWAGROUPMEMBER g on g.Z_PK = m.ZGROUPMEMBER
            left join ZWAMEDIAITEM i on i.Z_PK = m.ZMEDIAITEM
            left join ZWAMESSAGEINFO info on info.Z_PK = m.ZMESSAGEINFO
            where {where} order by m.Z_PK""", params):
        body = build_body(r["ZMESSAGETYPE"], r["ZTEXT"], r["ZTITLE"], r["ZMOVIEDURATION"])
        yield {
            "pk": r["Z_PK"], "stanza": r["ZSTANZAID"], "chat": r["ZCHATSESSION"], "is_from_me": bool(r["ZISFROMME"]),
            "sent_at": to_unix(r["ZMESSAGEDATE"]), "type": r["ZMESSAGETYPE"], "body": body,
            "sender_jid": r["member_jid"] or r["ZFROMJID"], "push_name": r["ZPUSHNAME"],
            "reply_to": quoted_stanza(r["ZMETADATA"]), "reactions": reactions(r["ZRECEIPTINFO"]),
        }
