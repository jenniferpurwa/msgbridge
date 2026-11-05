"""Builds a small fake chat.db with every case the tools must handle.

Uses the plain `text` column (older macOS format) so no typedstream encoding is
needed; decoding of attributedBody is covered by the M0 spike on real data.
"""
import sqlite3
import time
from pathlib import Path

from msgbridge.contacts import Contact
from msgbridge.imessage import from_unix

SCHEMA = """
create table handle(ROWID integer primary key, id text, service text);
create table chat(ROWID integer primary key, guid text, style integer, chat_identifier text,
                  display_name text, service_name text, group_id text, original_group_id text);
create table chat_handle_join(chat_id integer, handle_id integer);
create table chat_message_join(chat_id integer, message_id integer);
create table attachment(ROWID integer primary key, mime_type text, uti text, transfer_name text,
                        total_bytes integer, is_sticker integer default 0, filename text);
create table message_attachment_join(message_id integer, attachment_id integer);
create table chat_recoverable_message_join(chat_id integer, message_id integer);
create table message(ROWID integer primary key, guid text, text text, attributedBody blob,
    handle_id integer default 0, date integer, is_from_me integer default 0, item_type integer default 0,
    associated_message_guid text, associated_message_type integer default 0, associated_message_emoji text,
    thread_originator_guid text, date_edited integer default 0, date_retracted integer default 0,
    message_summary_info blob, balloon_bundle_id text, payload_data blob,
    cache_has_attachments integer default 0, is_audio_message integer default 0, service text default 'iMessage',
    group_action_type integer default 0, other_handle integer default 0);
"""

CONTACTS = [
    Contact("Stephen Lee", ["Steph"], ["+1 (415) 555-0101"]),
    Contact("Stephen Park", [], ["415-555-0102"]),
    Contact("Maya Chen", [], ["maya@example.com", "(415) 555-0103"]),
    Contact("Alex Kim", [], ["+14155550104"]),
]

HANDLES = {1: "+14155550101", 2: "+14155550102", 3: "maya@example.com", 4: "+14155550104",
           5: "+14155550103", 6: "12345"}
# chat rowid: (style, display_name, handle ids)
CHATS = {1: (45, None, [1]), 2: (45, None, [1]),           # Stephen Lee on iMessage and SMS
         3: (45, None, [2]), 4: (45, None, [3]),            # Stephen Park, Maya
         5: (43, "Pickleball crew", [1, 3, 4]),             # named group
         6: (43, None, [1, 5, 4, 2]),                       # unnamed group
         7: (45, None, [6]), 8: (43, "Bali trip", [3, 4]),  # bank short code, second named group
         9: (43, "Brunch club", [1, 3]),                    # iMessage group...
         10: (43, "Brunch club", [1, 5, 2]),                # ...continued as RCS after Stephen Park joined
         11: (43, None, [2])}                               # linked, but different people: stays separate
GROUP_IDS = {9: ("G-brunch-1", None), 10: ("G-brunch-2", "G-brunch-1"), 11: ("G-other", "G-brunch-1")}

DAY = 86400


class FakeChatDB:
    def __init__(self, path: Path):
        self.path = path
        self.con = sqlite3.connect(path)
        self.con.executescript(SCHEMA)
        self.con.executemany("insert into handle values (?, ?, 'iMessage')", HANDLES.items())
        for cid, (style, name, hids) in CHATS.items():
            gid, orig = GROUP_IDS.get(cid, (None, None))
            self.con.execute("insert into chat values (?, ?, ?, ?, ?, 'iMessage', ?, ?)",
                             (cid, f"chat{cid}", style, f"c{cid}", name, gid, orig))
            self.con.executemany("insert into chat_handle_join values (?, ?)", [(cid, h) for h in hids])
        self.next_rowid = 1
        self.now = time.time()
        self.con.commit()

    def add(self, chat, text, handle=0, me=False, days_ago=0.0, **cols) -> int:
        rid = self.next_rowid
        self.next_rowid += 1
        row = {"ROWID": rid, "guid": f"G{rid}", "text": text, "handle_id": handle,
               "date": from_unix(self.now - days_ago * DAY), "is_from_me": int(me), **cols}
        self.con.execute(f"insert into message({','.join(row)}) values ({','.join('?' * len(row))})", list(row.values()))
        if chat:
            self.con.execute("insert into chat_message_join values (?, ?)", (chat, rid))
        self.con.commit()
        return rid

    def attach(self, rid, mime, uti, name, sticker=0):
        aid = self.con.execute("insert into attachment(mime_type, uti, transfer_name, is_sticker) values (?, ?, ?, ?)",
                               (mime, uti, name, sticker)).lastrowid
        self.con.execute("insert into message_attachment_join values (?, ?)", (rid, aid))
        self.con.commit()


def build(path: Path) -> FakeChatDB:
    db = FakeChatDB(path)
    # Stephen Lee 1:1, split across iMessage (chat 1) and SMS (chat 2)
    db.add(1, "are we still doing pickleball saturday?", handle=1, days_ago=3)
    yes = db.add(1, "yes!! 10am at the usual courts", me=True, days_ago=2.9)
    db.add(1, "", handle=1, days_ago=2.8, associated_message_type=2000, associated_message_guid=f"p:0/G{yes}")
    db.add(2, "can we push to 11?", handle=1, days_ago=2.5, service="SMS", thread_originator_guid=f"G{yes}")
    photo = db.add(2, "￼", handle=1, days_ago=2.4, service="SMS")
    db.attach(photo, "image/heic", "public.heic", "IMG_1.heic")
    db.add(1, "sure 11 works, see you there", me=True, days_ago=2.3, date_edited=1)
    db.add(1, "oops wrong chat", me=True, days_ago=2.2, date_retracted=1)
    db.add(1, "check this https://www.nytimes.com/2026/10/bali-guide", handle=1, days_ago=1,
           balloon_bundle_id="com.apple.messages.URLBalloonProvider")
    # Stephen Park 1:1: last message inbound -> unreplied
    db.add(3, "hey are you free for coffee next week?", handle=2, days_ago=4)
    # Maya 1:1
    db.add(4, "loved dinner last night", handle=3, days_ago=6)
    db.add(4, "same!! lets do the thai place again", me=True, days_ago=5.9)
    # Pickleball group
    db.add(5, "who's bringing balls", handle=3, days_ago=2)
    db.add(5, "I got it", handle=1, days_ago=1.9)
    voice = db.add(5, "", handle=4, days_ago=1.8, is_audio_message=1)
    db.attach(voice, "audio/x-caf", "com.apple.coreaudio-format", "Audio Message.caf")
    # Unnamed group
    db.add(6, "group dinner friday?", handle=5, days_ago=3)
    db.add(6, "in", handle=1, days_ago=2.9)
    db.add(6, "me too", handle=1, days_ago=2.8)
    # Bali trip group
    db.add(8, "flights to Bali are cheap in March", handle=4, days_ago=20)
    db.add(8, "booking the villa in Ubud", me=True, days_ago=19)
    # Bank short code with a one-time code
    db.add(7, "Your verification code is 482913. Do not share it.", handle=6, days_ago=0.5)
    # Brunch club: one group in two linked chats, with a recorded add and a later removal
    db.add(9, "brunch sunday?", handle=1, days_ago=10)
    db.add(9, "", handle=0, me=True, days_ago=9.5, item_type=1, group_action_type=0, other_handle=4)  # I add Alex
    db.add(9, "yes!", handle=3, days_ago=9)
    db.add(10, "welcome stephen p!", handle=1, days_ago=8, service="RCS")
    db.add(9, "still getting some here", handle=3, days_ago=7.5)
    db.add(10, "see you all at 11", handle=2, days_ago=7, service="RCS")
    db.add(9, "", handle=0, me=True, days_ago=6, item_type=1, group_action_type=1, other_handle=4)   # I remove Alex
    db.add(11, "side chat", handle=2, days_ago=5)
    # Edge cases the importer must skip or attach
    db.add(None, "message with no chat row", handle=4, days_ago=7)       # orphan -> Alex 1:1
    db.add(5, "", handle=3, days_ago=1.5, item_type=2)                    # group rename event
    db.add(4, "", handle=3, days_ago=5)                                   # blank in source
    deleted = db.add(4, "deleted for real", handle=3, days_ago=1)
    db.con.execute("insert into chat_recoverable_message_join values (4, ?)", (deleted,))
    db.con.commit()
    return db
