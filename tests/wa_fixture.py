"""A small fake WhatsApp for Mac store: ChatStorage.sqlite plus ContactsV2.sqlite.

Reactions and reply links are encoded as protobuf with the field numbers the M3
spike found in the real database.
"""
import sqlite3
import time
from pathlib import Path

from msgbridge.whatsapp import from_unix

DAY = 86400


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def pb(*pairs) -> bytes:
    out = b""
    for num, value in pairs:
        if isinstance(value, int):
            out += _varint(num << 3) + _varint(value)
        else:
            data = value.encode() if isinstance(value, str) else value
            out += _varint(num << 3 | 2) + _varint(len(data)) + data
    return out


def receipt(*reactions) -> bytes:
    """reactions: (reactor_jid, emoji, unix_time); jid "" is you, stored without a reactor field as in WhatsApp."""
    records = [(1, pb((1, f"r{i}"), *([(2, jid)] if jid else []), (3, emoji), (4, int(ts * 1000)), (5, 1), (7, 0)))
               for i, (jid, emoji, ts) in enumerate(reactions)]
    return pb((7, pb(*records)))


CHAT_SCHEMA = """
create table ZWACHATSESSION(Z_PK integer primary key, ZSESSIONTYPE integer, ZCONTACTJID text, ZPARTNERNAME text,
                            ZREMOVED integer default 0);
create table ZWAGROUPMEMBER(Z_PK integer primary key, ZCHATSESSION integer, ZMEMBERJID text, ZCONTACTNAME text, ZFIRSTNAME text);
create table ZWAMEDIAITEM(Z_PK integer primary key, ZTITLE text, ZMOVIEDURATION integer, ZMETADATA blob);
create table ZWAMESSAGEINFO(Z_PK integer primary key, ZRECEIPTINFO blob);
create table ZWAPROFILEPUSHNAME(Z_PK integer primary key, ZJID text, ZPUSHNAME text);
create table ZWAMESSAGE(Z_PK integer primary key, ZSTANZAID text, ZCHATSESSION integer, ZISFROMME integer,
    ZMESSAGEDATE real, ZMESSAGETYPE integer, ZTEXT text, ZFROMJID text, ZPUSHNAME text, ZGROUPMEMBER integer,
    ZMEDIAITEM integer, ZMESSAGEINFO integer);
"""

STEPHEN = "14155550101@s.whatsapp.net"      # same phone as Stephen Lee in Contacts and iMessage
MAYA_LID = "222222222222222@lid"            # privacy ID; ContactsV2 maps it to Maya's phone
JORDAN_LID = "333333333333333@lid"          # not in Contacts: only a push name
RILEY_LID = "444444444444444@lid"           # no name anywhere until a message carries one


class FakeWhatsApp:
    def __init__(self, folder: Path):
        folder.mkdir(parents=True, exist_ok=True)
        self.dir = folder
        c2 = sqlite3.connect(folder / "ContactsV2.sqlite")
        c2.executescript("create table ZWAADDRESSBOOKCONTACT(Z_PK integer primary key, ZLID text, ZWHATSAPPID text);")
        c2.execute("insert into ZWAADDRESSBOOKCONTACT(ZLID, ZWHATSAPPID) values (?, ?)", (MAYA_LID, "14155550103@s.whatsapp.net"))
        c2.commit()
        c2.close()
        self.con = sqlite3.connect(folder / "ChatStorage.sqlite")
        self.con.executescript(CHAT_SCHEMA)
        self.con.executemany("insert into ZWACHATSESSION(Z_PK, ZSESSIONTYPE, ZCONTACTJID, ZPARTNERNAME) values (?, ?, ?, ?)", [
            (1, 0, STEPHEN, "Stephen"),
            (2, 1, "120363000000001@g.us", "Hiking crew"),
            (3, 3, "status@broadcast", None),
            (4, 0, MAYA_LID, "Maya C"),
        ])
        self.con.executemany("insert into ZWAGROUPMEMBER(Z_PK, ZCHATSESSION, ZMEMBERJID, ZCONTACTNAME) values (?, 2, ?, ?)", [
            (1, STEPHEN, "Stephen"), (2, MAYA_LID, "Maya C"), (3, JORDAN_LID, None), (4, RILEY_LID, None)])
        self.con.execute("insert into ZWAPROFILEPUSHNAME(ZJID, ZPUSHNAME) values (?, 'Jordan')", (JORDAN_LID,))
        self.now = time.time()
        self.con.commit()

    def add(self, chat, mtype, text=None, me=False, days_ago=0.0, sender=None, member=None, push=None,
            title=None, duration=None, reply_to=None, reactions=()):
        media = info = None
        if title or duration or reply_to:
            media = self.con.execute("insert into ZWAMEDIAITEM(ZTITLE, ZMOVIEDURATION, ZMETADATA) values (?, ?, ?)",
                                     (title, duration, pb((5, reply_to)) if reply_to else None)).lastrowid
        if reactions:
            info = self.con.execute("insert into ZWAMESSAGEINFO(ZRECEIPTINFO) values (?)", (receipt(*reactions),)).lastrowid
        pk = self.con.execute("""insert into ZWAMESSAGE(ZSTANZAID, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZMESSAGETYPE,
                                 ZTEXT, ZFROMJID, ZPUSHNAME, ZGROUPMEMBER, ZMEDIAITEM, ZMESSAGEINFO)
                                 values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                              (None, chat, int(me), from_unix(self.now - days_ago * DAY), mtype, text,
                               None if me else sender, push, member, media, info)).lastrowid
        stanza = f"S{pk}"
        self.con.execute("update ZWAMESSAGE set ZSTANZAID = ? where Z_PK = ?", (stanza, pk))
        self.con.commit()
        return stanza


def build(folder: Path) -> FakeWhatsApp:
    wa = FakeWhatsApp(folder)
    t = wa.now
    # 1:1 with Stephen (same person as the iMessage thread)
    q = wa.add(1, 0, "are you coming to the hike on sunday?", sender=STEPHEN, days_ago=1.5,
               reactions=[("", "👍", t - 1.4 * DAY)])
    wa.add(1, 0, "yes! what time", me=True, days_ago=1.4, reply_to=q)
    wa.add(1, 3, me=False, sender=STEPHEN, days_ago=1.3, duration=42)
    wa.add(1, 7, "https://www.alltrails.com/trail/mission-peak", sender=STEPHEN, days_ago=1.2, title="Mission Peak Trail")
    wa.add(1, 59, sender=STEPHEN, days_ago=1.1)                     # undocumented type, no text
    # Hiking group: names come from Contacts (via phone or LID map) or push names
    mine = wa.add(2, 0, "who's driving?", me=True, days_ago=1.0,
                  reactions=[(MAYA_LID, "❤️", t - 0.9 * DAY), (JORDAN_LID, "😂", t - 0.9 * DAY)])
    wa.add(2, 0, "I can drive", sender="120363000000001@g.us", member=2, days_ago=0.9)
    wa.add(2, 1, sender="120363000000001@g.us", member=3, push="Jordan", days_ago=0.8, title="the view from last time")
    wa.add(2, 6, sender="120363000000001@g.us", member=1, days_ago=0.7)   # group event: skipped
    wa.add(2, 0, "count me in", sender="120363000000001@g.us", member=4, push="Riley", days_ago=0.6)
    # Maya 1:1 via privacy ID
    wa.add(4, 0, "send me the trail link?", sender=MAYA_LID, days_ago=0.5)
    # Status update: not a conversation
    wa.add(3, 0, "status text", sender=STEPHEN, days_ago=0.4)
    assert mine
    return wa
