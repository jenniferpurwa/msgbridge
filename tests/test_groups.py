"""Group chats that Messages split into linked chats, and membership changes."""
import pytest

from msgbridge.index import Config, Index
from msgbridge.tools import Tools

from .fixture import CONTACTS, build


@pytest.fixture
def env(tmp_path):
    fake = build(tmp_path / "chat.db")
    index = Index(Config(index_path=tmp_path / "index.db", chat_db=fake.path, contacts=CONTACTS,
                         wa_dir=tmp_path / "no-whatsapp"))
    index.rebuild()
    return fake, index, Tools(index, auto_sync=False)


def brunch(index):
    return [r[0] for r in index.db.execute("select id from thread where name = 'Brunch club'")]


def test_linked_group_chats_merge_into_one_timeline(env):
    _, index, tools = env
    threads = brunch(index)
    assert len(threads) == 1  # two linked chats, one thread
    out = tools.get_messages(threads[0])
    assert "via RCS + iMessage" in out
    texts = [l.split(": ", 1)[-1] for l in out.splitlines() if l[:2].isdigit()]
    assert texts.index("welcome stephen p!") < texts.index("still getting some here") < texts.index("see you all at 11")


def test_membership_changes_in_timeline_and_header(env):
    _, index, tools = env
    out = tools.get_messages(brunch(index)[0])
    assert "[Alex joined the group, added by Me]" in out
    assert "[Me removed Alex]" in out
    assert "Stephen Park joined the group (first seen when Messages started a new copy of the group chat)" in out
    header = next(l for l in out.splitlines() if l.startswith("MEMBER CHANGES:"))
    assert "Stephen Park joined the group" in header and "Me removed Alex" in header


def test_linked_chat_with_different_people_stays_separate(env):
    _, index, _ = env
    side = index.db.execute("select thread_id from thread_source where chat_rowid = 11").fetchone()[0]
    assert side != brunch(index)[0]


def test_new_linked_chat_joins_existing_thread_on_sync(env):
    fake, index, tools = env
    before = brunch(index)[0]
    fake.con.execute("insert into chat values (12, 'chat12', 43, 'c12', 'Brunch club', 'iMessage', 'G-brunch-3', 'G-brunch-2')")
    fake.con.executemany("insert into chat_handle_join values (12, ?)", [(1,), (5,), (2,)])
    fake.con.commit()
    fake.add(12, "third copy of the chat", handle=1, days_ago=0)
    index.sync()
    assert brunch(index) == [before]
    assert "third copy of the chat" in tools.get_messages(before)
