"""The msgbridge index: one SQLite file the tools query instead of the sources.

`rebuild()` imports everything from scratch; `sync()` picks up new rows and
re-reads the last 7 days so edits, unsends and reactions are caught. The index
can always be deleted and rebuilt from chat.db and Contacts.
"""
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import imessage, whatsapp
from .contacts import Contact, load_addressbook
from .identity import is_business, normalize, pretty
from .redact import redact

RESYNC_WINDOW = 7 * 86400
WA_OFFSET = 1 << 40           # WhatsApp message ids live above every chat.db ROWID
WA_REACTION_BASE = 1 << 50    # synthetic ids for WhatsApp reactions (stored as current state, not events)

SCHEMA = """
create table if not exists person(
    id integer primary key, name text not null, nicknames text not null default '',
    from_contacts integer not null default 0);
create table if not exists identity(value text primary key, person_id integer not null);
create table if not exists handle_map(
    handle_rowid integer primary key, person_id integer not null, business integer not null);
create table if not exists thread(
    id text primary key, app text not null, kind text not null, name text, label text not null,
    business integer not null default 0, message_count integer not null default 0,
    last_message_at real, last_inbound_at real, last_outbound_at real, last_is_inbound integer);
create table if not exists thread_source(chat_rowid integer primary key, thread_id text not null);
create table if not exists thread_member(
    thread_id text not null, person_id integer not null, primary key(thread_id, person_id));
create table if not exists message(
    src_rowid integer primary key, guid text unique, thread_id text not null, sender_id integer,
    is_from_me integer not null, sent_at real not null, body text not null,
    edited integer not null, unsent integer not null, reply_to_guid text, service text);
create index if not exists message_thread_time on message(thread_id, sent_at);
create index if not exists message_time on message(sent_at);
create table if not exists reaction(
    src_rowid integer primary key, target_guid text, sender_id integer, is_from_me integer,
    emoji text, removed integer, sent_at real);
create index if not exists reaction_target on reaction(target_guid);
create virtual table if not exists message_fts using fts5(
    body, content='message', content_rowid='src_rowid', tokenize='unicode61 remove_diacritics 2');
create trigger if not exists message_ai after insert on message begin
    insert into message_fts(rowid, body) values (new.src_rowid, new.body); end;
create trigger if not exists message_ad after delete on message begin
    insert into message_fts(message_fts, rowid, body) values ('delete', old.src_rowid, old.body); end;
create trigger if not exists message_au after update of body on message begin
    insert into message_fts(message_fts, rowid, body) values ('delete', old.src_rowid, old.body);
    insert into message_fts(rowid, body) values (new.src_rowid, new.body); end;
create table if not exists meta(key text primary key, value text);
create table if not exists wa_jid(jid text primary key, person_id integer not null);
"""


@dataclass
class Config:
    index_path: Path
    chat_db: Path = Path.home() / "Library/Messages/chat.db"
    addressbook_dir: Path = Path.home() / "Library/Application Support/AddressBook"
    wa_dir: Path = whatsapp.DEFAULT_DIR
    region: str = "US"
    contacts: list[Contact] | None = field(default=None, repr=False)  # tests inject these

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            index_path=Path(os.environ.get(
                "MSGBRIDGE_INDEX", Path.home() / "Library/Application Support/MsgBridge/index.db")),
            chat_db=Path(os.environ.get("MSGBRIDGE_CHAT_DB", Path.home() / "Library/Messages/chat.db")),
            wa_dir=Path(os.environ.get("MSGBRIDGE_WA_DIR", whatsapp.DEFAULT_DIR)),
            region=os.environ.get("MSGBRIDGE_REGION", "US"))


class Index:
    def __init__(self, config: Config):
        self.config = config
        config.index_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # The MCP SDK calls tools from worker threads; the server serializes calls with a lock.
        self.db = sqlite3.connect(config.index_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        os.chmod(config.index_path, 0o600)

    # -- meta -------------------------------------------------------------
    def meta(self, key, default=None):
        row = self.db.execute("select value from meta where key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key, value):
        self.db.execute("insert into meta values (?, ?) on conflict(key) do update set value = excluded.value",
                        (key, str(value)))

    # -- import -----------------------------------------------------------
    def rebuild(self) -> dict:
        started = time.time()
        stats = self._import(full=True, reset=True)
        stats["seconds"] = round(time.time() - started, 1)
        return stats

    def _reset(self):
        for table in ("person", "identity", "handle_map", "thread", "thread_source",
                      "thread_member", "message", "reaction", "meta", "wa_jid"):
            self.db.execute(f"delete from {table}")
        self.db.execute("insert into message_fts(message_fts) values ('rebuild')")
        self._load_contacts()

    def sync(self) -> dict:
        if not self.meta("last_rowid"):
            return self.rebuild()
        return self._import(full=False)

    def _load_contacts(self):
        contacts = (self.config.contacts if self.config.contacts is not None
                    else load_addressbook(self.config.addressbook_dir))
        for c in contacts:
            keys = {normalize(h, self.config.region) for h in c.handles}
            existing = {r[0] for k in keys
                        for r in self.db.execute("select person_id from identity where value = ?", (k,))}
            if existing:  # the same person on two contact sources: merge into the first card
                pid = min(existing)
            else:
                pid = self.db.execute("insert into person(name, nicknames, from_contacts) values (?, ?, 1)",
                                      (c.name, "\n".join(c.nicknames))).lastrowid
            for k in keys:
                self.db.execute("insert or ignore into identity values (?, ?)", (k, pid))

    def _person_for_handle(self, handle_rowid: int, handle: str) -> tuple[int, bool]:
        row = self.db.execute("select person_id, business from handle_map where handle_rowid = ?",
                              (handle_rowid,)).fetchone()
        if row:
            return row[0], bool(row[1])
        key = normalize(handle, self.config.region)
        hit = self.db.execute("select person_id from identity where value = ?", (key,)).fetchone()
        if hit:
            pid, business = hit[0], False
        else:
            pid = self.db.execute("insert into person(name) values (?)",
                                  (pretty(handle, self.config.region),)).lastrowid
            self.db.execute("insert or ignore into identity values (?, ?)", (key, pid))
            business = is_business(handle)
        self.db.execute("insert into handle_map values (?, ?, ?)", (handle_rowid, pid, int(business)))
        return pid, business

    def _map_chats(self, src) -> None:
        handles = imessage.read_handles(src)
        for hid, h in handles.items():
            self._person_for_handle(hid, h)
        chat_handles = imessage.read_chat_handles(src)
        known = {r[0] for r in self.db.execute("select chat_rowid from thread_source")}
        for chat in imessage.read_chats(src):
            if chat["ROWID"] in known:
                continue
            members = [self._person_for_handle(h, handles[h])
                       for h in chat_handles.get(chat["ROWID"], []) if h in handles]
            if chat["style"] == imessage.GROUP_STYLE:
                tid = f"t_g{chat['ROWID']}"
                name = chat["display_name"] or None
                self.db.execute("insert or ignore into thread(id, app, kind, name, label) values (?, 'iMessage', 'group', ?, ?)",
                                (tid, name, name or "group"))
            elif members:
                pid, business = members[0]
                tid = self._direct_thread(pid, business)
            else:
                continue
            self.db.execute("insert into thread_source values (?, ?)", (chat["ROWID"], tid))
            for pid, _ in members:
                self.db.execute("insert or ignore into thread_member values (?, ?)", (tid, pid))

    def _direct_thread(self, pid: int, business: bool, app: str = "iMessage") -> str:
        """iMessage, SMS and RCS chats with one person share one 1:1 thread; WhatsApp gets its own."""
        tid = f"t_p{pid}" if app == "iMessage" else f"t_wp{pid}"
        name = self.db.execute("select name from person where id = ?", (pid,)).fetchone()[0]
        self.db.execute("insert or ignore into thread(id, app, kind, name, label, business) values (?, ?, 'direct', ?, ?, ?)",
                        (tid, app, name, name, int(business)))
        self.db.execute("insert or ignore into thread_member values (?, ?)", (tid, pid))
        return tid

    def _import(self, full: bool, reset: bool = False) -> dict:
        src = imessage.connect(self.config.chat_db)
        counts = {"messages": 0, "reactions": 0, "skipped_no_thread": 0}
        try:
            with self.db:  # one transaction: a failed rebuild leaves the previous index untouched
                if reset:
                    self._reset()
                self._map_chats(src)
                chat_thread = dict(self.db.execute("select chat_rowid, thread_id from thread_source").fetchall())
                handle_person = {r[0]: (r[1], bool(r[2])) for r in self.db.execute("select * from handle_map")}
                last_rowid = int(self.meta("last_rowid", 0))
                batches = [imessage.read_messages(src, min_rowid=last_rowid)]
                if not full:
                    since = imessage.from_unix(time.time() - RESYNC_WINDOW)
                    batches.append(imessage.read_messages(src, since_apple=since))
                max_rowid = last_rowid
                for batch in batches:
                    for m in batch:
                        max_rowid = max(max_rowid, m["rowid"])
                        sender, business = handle_person.get(m["handle_id"], (None, False))
                        tid = chat_thread.get(m["chat_id"])
                        if tid is None and sender and m["chat_id"] is None:
                            tid = self._direct_thread(sender, business)  # message outside any chat
                        if tid is None:
                            counts["skipped_no_thread"] += 1
                            continue
                        sender_id = None if m["is_from_me"] else sender
                        if m["type"] == "reaction":
                            self.db.execute("""insert or replace into reaction values (?, ?, ?, ?, ?, ?, ?)""",
                                            (m["rowid"], m["target_guid"], sender_id, int(m["is_from_me"]),
                                             m["emoji"], int(m["removed"]), m["sent_at"]))
                            counts["reactions"] += 1
                            continue
                        self.db.execute("""
                            insert into message values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            on conflict(src_rowid) do update set body = excluded.body,
                                edited = excluded.edited, unsent = excluded.unsent""",
                            (m["rowid"], m["guid"], tid, sender_id, int(m["is_from_me"]), m["sent_at"],
                             redact(m["body"], business), int(m["edited"]), int(m["unsent"]),
                             m["reply_to_guid"], m["service"]))
                        counts["messages"] += 1
                self.set_meta("last_rowid", max_rowid)
                self._import_whatsapp(full, counts)
                self._update_thread_stats()
                self.set_meta("last_sync_at", time.time())
        finally:
            src.close()
        return counts

    # -- WhatsApp -----------------------------------------------------------------
    def _wa_person(self, jid: str, name: str, lid_phones: dict, push: dict) -> int:
        """One person per JID; a phone number shared with Contacts or iMessage joins the same person."""
        row = self.db.execute("select person_id from wa_jid where jid = ?", (jid,)).fetchone()
        if row:
            better = name or push.get(jid)
            if better:  # a name seen later replaces the "WhatsApp user" placeholder
                self.db.execute("update person set name = ? where id = ? and name = 'WhatsApp user'", (better, row[0]))
            return row[0]
        phone = whatsapp.jid_phone(jid, lid_phones)
        key = normalize(phone, self.config.region) if phone else None
        hit = self.db.execute("select person_id from identity where value = ?", (key,)).fetchone() if key else None
        if hit:
            pid = hit[0]
        else:
            label = name or push.get(jid) or (pretty(phone, self.config.region) if phone else "WhatsApp user")
            pid = self.db.execute("insert into person(name) values (?)", (label,)).lastrowid
            if key:
                self.db.execute("insert or ignore into identity values (?, ?)", (key, pid))
        self.db.execute("insert into wa_jid values (?, ?)", (jid, pid))
        return pid

    def _import_whatsapp(self, full: bool, counts: dict) -> None:
        chat_db = self.config.wa_dir / "ChatStorage.sqlite"
        if not chat_db.exists():
            return
        src = whatsapp.connect(chat_db)
        try:
            lid_phones = whatsapp.read_lid_phones(self.config.wa_dir / "ContactsV2.sqlite")
            push = whatsapp.read_push_names(src)
            members = whatsapp.read_members(src)
            chat_thread, partner = {}, {}
            for c in whatsapp.read_chats(src):
                if c["ZSESSIONTYPE"] == whatsapp.DIRECT:
                    pid = self._wa_person(c["ZCONTACTJID"], c["ZPARTNERNAME"] or "", lid_phones, push)
                    chat_thread[c["Z_PK"]] = self._direct_thread(pid, False, app="WhatsApp")
                    partner[c["Z_PK"]] = (pid, c["ZCONTACTJID"])
                else:
                    tid, name = f"t_wg{c['Z_PK']}", (c["ZPARTNERNAME"] or None)
                    self.db.execute("insert or ignore into thread(id, app, kind, name, label) values (?, 'WhatsApp', 'group', ?, ?)",
                                    (tid, name, name or "group"))
                    chat_thread[c["Z_PK"]] = tid
                    for jid, member_name in members.get(c["Z_PK"], []):
                        pid = self._wa_person(jid, member_name, lid_phones, push)
                        self.db.execute("insert or ignore into thread_member values (?, ?)", (tid, pid))

            last_pk = int(self.meta("wa_last_pk", 0))
            batches = [whatsapp.read_messages(src, min_pk=last_pk)]
            if not full:
                batches.append(whatsapp.read_messages(src, since_apple=whatsapp.from_unix(time.time() - RESYNC_WINDOW)))
            max_pk = last_pk
            for batch in batches:
                for m in batch:
                    max_pk = max(max_pk, m["pk"])
                    tid = chat_thread.get(m["chat"])
                    if tid is None:
                        counts["skipped_no_thread"] += 1  # status updates and broadcasts
                        continue
                    guid = f"wa:{m['chat']}:{m['stanza'] or m['pk']}"  # stanza ids repeat across chats
                    owner = self.db.execute("select src_rowid from message where guid = ?", (guid,)).fetchone()
                    if owner and owner[0] != WA_OFFSET + m["pk"]:
                        guid += f"#{m['pk']}"  # and, rarely, within one chat
                    direct = partner.get(m["chat"])
                    self.db.execute("delete from reaction where target_guid = ? and src_rowid >= ?", (guid, WA_REACTION_BASE))
                    for k, (jid, emoji, ts) in enumerate(m["reactions"][:32]):
                        is_me = not jid  # your own reactions carry no reactor id
                        sender = None if is_me else (direct[0] if direct else self._wa_person(jid, "", lid_phones, push))
                        self.db.execute("insert or replace into reaction values (?, ?, ?, ?, ?, 0, ?)",
                                        (WA_REACTION_BASE + m["pk"] * 32 + k, guid, sender, int(is_me), emoji, ts))
                        counts["reactions"] += 1
                    if m["body"] is None:
                        continue
                    if m["is_from_me"]:
                        sender_id = None
                    elif direct:
                        sender_id = direct[0]
                    else:
                        sender_id = self._wa_person(m["sender_jid"] or "", m["push_name"] or "", lid_phones, push)
                    self.db.execute("""
                        insert into message values (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, 'WhatsApp')
                        on conflict(src_rowid) do update set body = excluded.body""",
                        (WA_OFFSET + m["pk"], guid, tid, sender_id, int(m["is_from_me"]), m["sent_at"],
                         redact(m["body"], False), f"wa:{m['chat']}:{m['reply_to']}" if m["reply_to"] else None))
                    counts["messages"] += 1
            self.set_meta("wa_last_pk", max_pk)
        finally:
            src.close()

    def _update_thread_stats(self):
        self.db.execute("""
            update thread set
                message_count = s.n, last_message_at = s.last_at,
                last_inbound_at = s.last_in, last_outbound_at = s.last_out
            from (select thread_id, count(*) n, max(sent_at) last_at,
                         max(case when is_from_me = 0 then sent_at end) last_in,
                         max(case when is_from_me = 1 then sent_at end) last_out
                  from message group by thread_id) s
            where s.thread_id = thread.id""")
        self.db.execute("update thread set last_is_inbound = (last_inbound_at >= coalesce(last_outbound_at, 0))"
                        " where last_inbound_at is not null")
        self.db.execute("update thread set last_is_inbound = 0 where last_inbound_at is null")
        self._label_unnamed_groups()

    def _label_unnamed_groups(self):
        """'Stephen, Maya +2': the two members who wrote most, then a count of the rest."""
        for (tid,) in self.db.execute("select id from thread where kind = 'group' and name is null").fetchall():
            names = [r[0] for r in self.db.execute("""
                select p.name from thread_member tm join person p on p.id = tm.person_id
                left join (select sender_id, count(*) n from message where thread_id = ? group by sender_id) c
                    on c.sender_id = tm.person_id
                where tm.thread_id = ? order by coalesce(c.n, 0) desc, p.name""", (tid, tid))]
            first = [n.split(" ")[0] if any(ch.isalpha() for ch in n) else n for n in names[:2]]
            label = ", ".join(first) + (f" +{len(names) - 2}" if len(names) > 2 else "") if names else "Unnamed group"
            self.db.execute("update thread set label = ? where id = ?", (label, tid))
