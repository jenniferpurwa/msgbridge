import sqlite3

import pytest

from msgbridge.imessage import connect
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


def thread_of(index, name):
    return index.db.execute("select t.id from thread t where t.kind = 'direct' and t.label = ?", (name,)).fetchone()[0]


def test_source_is_read_only(env):
    fake, _, _ = env
    with pytest.raises(sqlite3.OperationalError):
        connect(fake.path).execute("insert into handle values (99, 'x', 'x')")


def test_imessage_and_sms_chats_merge_into_one_thread(env):
    _, index, tools = env
    out = tools.get_messages(thread_of(index, "Stephen Lee"))
    assert "via SMS + iMessage" in out
    assert "can we push to 11?" in out and "are we still doing pickleball" in out


def test_placeholders_replies_reactions_edits(env):
    _, index, tools = env
    out = tools.get_messages(thread_of(index, "Stephen Lee"), format="full")
    assert 'Me: yes!! 10am at the usual courts (Stephen Lee reacted ❤️)' in out
    assert 'Stephen Lee (replying to Me: "yes!! 10am at the usual courts"): can we push to 11?' in out
    assert "Stephen Lee: [photo]" in out
    assert "see you there (edited)" in out
    assert "Me: [unsent message]" in out
    assert "check this https://www.nytimes.com/2026/10/bali-guide" in out
    assert "￼" not in out


def test_header_names_source_and_range(env):
    _, index, tools = env
    out = tools.get_messages(thread_of(index, "Stephen Lee"), limit=3)
    assert out.startswith("SOURCE: iMessage · 1:1 with Stephen Lee")
    assert "RETURNED: 3 messages" in out
    assert "OLDER: 4 earlier messages available" in out
    cursor = out.split('before="')[1].split('"')[0]
    page2 = tools.get_messages(thread_of(index, "Stephen Lee"), limit=10, before=cursor)
    assert "RETURNED: 4 messages" in page2 and "OLDER: none" in page2


def test_find_prefers_direct_thread_then_groups(env):
    _, _, tools = env
    out = tools.find_conversations("Steph")  # Stephen Lee's saved nickname
    lines = out.splitlines()
    assert "1:1 with Stephen Lee" in lines[1]
    assert "Pickleball crew" in out
    assert "AMBIGUOUS: no" in out
    both = tools.find_conversations("Stephen")
    assert "AMBIGUOUS: yes" in both and "Stephen Park" in both


def test_find_nickname_and_exact_name(env):
    _, _, tools = env
    out = tools.find_conversations("Maya")
    assert "1:1 with Maya Chen" in out.splitlines()[1]
    assert "AMBIGUOUS: no" in out


def test_find_group_by_name_and_members(env):
    _, _, tools = env
    out = tools.find_conversations("pickleball", members=["Stephen Lee"], kind="group")
    assert 'group "Pickleball crew"' in out.splitlines()[1]
    both = tools.find_conversations(members=["Stephen Lee", "Maya"], kind="group")
    assert "Pickleball crew" in both and "Bali trip" not in both
    assert "Bali trip" in tools.find_conversations("bali")


def test_unnamed_group_gets_member_label(env):
    _, index, _ = env
    label = index.db.execute("select label from thread where kind = 'group' and name is null").fetchone()[0]
    assert label == "Stephen, Maya +2"


def test_search_across_threads_for_a_person(env):
    _, _, tools = env
    out = tools.search_messages("Bali", person="Maya")
    assert "flights to Bali" in out and 'group "Bali trip"' in out
    assert "nytimes" not in out  # link text is a placeholder, and Maya is not in that thread


def test_context_marks_the_target(env):
    _, _, tools = env
    hit = tools.search_messages("bringing balls")
    mid = [w for w in hit.split() if w.startswith("m_")][0]
    out = tools.get_message_context(mid, before=2, after=2)
    assert ">> " in out and "Maya: who's bringing balls" in out and "Stephen: I got it" in out


def test_style_examples_are_my_messages(env):
    _, index, tools = env
    out = tools.get_style_examples(thread_of(index, "Stephen Lee"))
    assert "to Stephen Lee: yes!! 10am at the usual courts" in out
    assert "from this thread" in out and "[unsent" not in out
    assert "to Maya Chen: same!! lets do the thai place again" in out  # filled from other chats


def test_unreplied_skips_businesses_and_answered(env):
    _, _, tools = env
    out = tools.list_unreplied(since_days=30)
    assert "Stephen Park" in out
    assert "Maya Chen" not in out  # I replied last
    assert "12345" not in out


def test_one_time_codes_are_masked(env):
    _, index, _ = env
    body = index.db.execute("select body from message where body like '%verification%'").fetchone()[0]
    assert "482913" not in body and "[code]" in body


def test_skips_system_blank_and_deleted_and_attaches_orphans(env):
    _, index, tools = env
    bodies = [r[0] for r in index.db.execute("select body from message")]
    assert "deleted for real" not in bodies
    assert "" not in bodies
    assert "message with no chat row" in tools.get_messages(thread_of(index, "Alex Kim"))


def test_voice_note_placeholder(env):
    _, _, tools = env
    out = tools.find_conversations("pickleball")
    tid = out.splitlines()[1].split(" · ")[0].split(". ")[1]
    assert "Alex: [voice note]" in tools.get_messages(tid)


def test_incremental_sync_picks_up_new_and_edited(env):
    fake, index, tools = env
    tid = thread_of(index, "Maya Chen")
    fake.add(4, "new message after sync", handle=3, days_ago=0)
    fake.con.execute("update message set text = 'loved dinner last night!!', date_edited = 1 "
                     "where text = 'loved dinner last night'")
    fake.con.commit()
    index.sync()
    out = tools.get_messages(tid)
    assert "new message after sync" in out
    assert "loved dinner last night!! (edited)" in out
    assert tools.search_messages("after sync").count("m_") == 1


def test_compact_format_has_day_headers_and_first_names(env):
    _, index, tools = env
    out = tools.get_messages(thread_of(index, "Stephen Lee"))
    body = out.split("---\n", 1)[1]
    assert body.splitlines()[0].startswith("== ")
    assert "Stephen: are we still doing pickleball" in out
    assert "[" not in body.splitlines()[1].split(" ")[0]  # HH:MM, no bracketed date
    full = tools.get_messages(thread_of(index, "Stephen Lee"), format="full")
    assert len(out) < len(full)


def test_coverage_line(env):
    _, index, tools = env
    out = tools.get_messages(thread_of(index, "Stephen Lee"), limit=3)
    assert "COVERAGE: messages 5–7 of 7 matching these filters" in out
    cursor = out.split('before="')[1].split('"')[0]
    page2 = tools.get_messages(thread_of(index, "Stephen Lee"), limit=3, before=cursor)
    assert "COVERAGE: messages 2–4 of 7" in page2 and "3 newer were in earlier pages" in page2


def test_deep_read_needs_confirmation(env, monkeypatch):
    import msgbridge.tools as t
    _, index, tools = env
    monkeypatch.setattr(t, "MAX_LIMIT", 3)
    tid = thread_of(index, "Stephen Lee")
    ask = tools.get_messages(tid, limit=50)
    assert "CONFIRM NEEDED: this would read 7 messages" in ask and "tokens" in ask
    assert "--- MESSAGES" not in ask
    ok = tools.get_messages(tid, limit=50, confirm_large=True)
    assert "RETURNED: 7 messages" in ok
    capped = tools.get_messages(tid, limit=2)
    assert "RETURNED: 2 messages" in capped  # within the limit: no confirmation needed


def test_relationship_stats_counts_everything(env):
    _, _, tools = env
    out = tools.get_relationship_stats(since="2020-01-01")
    assert "no sampling" in out and "--- MESSAGES" not in out
    lee = next(l for l in out.splitlines() if l.split(" ", 1)[-1].startswith("Stephen Lee"))
    assert "1:1 messages 7 (them 4 / me 3" in lee
    assert "reactions 1 / 0" in lee                 # Stephen hearted my message
    assert "conversations started 3 / 0" in lee     # gaps of ~10 h and ~29 h start new conversations, both by Stephen
    assert "their group messages 5 in 3 shared active group(s)" in lee
    park = next(l for l in out.splitlines() if "Stephen Park" in l)
    assert "they send 100%" in park
    only = tools.get_relationship_stats(since="2020-01-01", people=["Maya"])
    assert "Maya Chen" in only and "Stephen" not in only
    assert "12345" not in out                       # businesses excluded
