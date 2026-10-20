"""Load names, nicknames, phones and emails from macOS Contacts.

Reads the AddressBook SQLite stores read-only (needs Full Disk Access, which the
indexer already has for chat.db). The spec's Swift CNContactStore helper can
replace this later without changing callers.
"""
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Contact:
    name: str
    nicknames: list[str] = field(default_factory=list)
    handles: list[str] = field(default_factory=list)  # raw phones and emails


def _stores(addressbook_dir: Path) -> list[Path]:
    return [p for p in [addressbook_dir / "AddressBook-v22.abcddb",
                        *sorted(addressbook_dir.glob("Sources/*/AddressBook-v22.abcddb"))] if p.exists()]


def load_addressbook(addressbook_dir: Path) -> list[Contact]:
    contacts = []
    for store in _stores(addressbook_dir):
        con = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
        try:
            handles: dict[int, list[str]] = {}
            for owner, num in con.execute("select ZOWNER, ZFULLNUMBER from ZABCDPHONENUMBER where ZFULLNUMBER is not null"):
                handles.setdefault(owner, []).append(num)
            for owner, addr in con.execute("select ZOWNER, ZADDRESS from ZABCDEMAILADDRESS where ZADDRESS is not null"):
                handles.setdefault(owner, []).append(addr)
            for pk, first, last, nick, org in con.execute(
                    "select Z_PK, ZFIRSTNAME, ZLASTNAME, ZNICKNAME, ZORGANIZATION from ZABCDRECORD"):
                if pk not in handles:
                    continue  # groups, or cards with no way to match a message
                name = " ".join(p for p in (first, last) if p) or org or nick
                if not name:
                    continue
                nicks = [n for n in (nick, first) if n and n != name]
                contacts.append(Contact(name=name, nicknames=nicks, handles=handles[pk]))
        finally:
            con.close()
    return contacts


def load_json(path: Path) -> list[Contact]:
    """Test fixtures: [{"name": ..., "nicknames": [...], "handles": [...]}]."""
    return [Contact(**c) for c in json.loads(Path(path).read_text())]
