import pytest

from msgbridge.index import Config, Index
from msgbridge.tools import Tools

from . import fixture, wa_fixture


@pytest.fixture
def env(tmp_path):
    fake = fixture.build(tmp_path / "chat.db")
    wa = wa_fixture.build(tmp_path / "wa")
    index = Index(Config(index_path=tmp_path / "index.db", chat_db=fake.path, contacts=fixture.CONTACTS,
                         wa_dir=wa.dir))
    index.rebuild()
    return wa, index, Tools(index, auto_sync=False)


def wa_thread(index, label):
    return index.db.execute("select id from thread where app = 'WhatsApp' and label = ?", (label,)).fetchone()[0]


def test_same_person_gets_two_threads(env):
    _, index, tools = env
    out = tools.find_conversations("Stephen Lee")
    lines = out.splitlines()
    assert any("t_p" in l and "iMessage" in l for l in lines)
    assert any("t_wp" in l and "WhatsApp" in l for l in lines)
    assert index.db.execute("select count(*) from person where name = 'Stephen Lee'").fetchone()[0] == 1


def test_privacy_id_maps_to_contact(env):
    _, _, tools = env
    out = tools.find_conversations("Maya")
    assert "WhatsApp" in out and "1:1 with Maya Chen" in out


def test_placeholders_replies_and_reactions(env):
    _, index, tools = env
    tid = index.db.execute("select id from thread where app = 'WhatsApp' and kind = 'direct' and label = 'Stephen Lee'").fetchone()[0]
    out = tools.get_messages(tid, format="full")
    assert out.startswith("SOURCE: WhatsApp · 1:1 with Stephen Lee")
    assert "are you coming to the hike on sunday? (Me reacted 👍)" in out
    assert 'Me (replying to Stephen Lee: "are you coming to the hike on sunday?"): yes! what time' in out
    assert "[voice note 0:42]" in out
    assert "https://www.alltrails.com/trail/mission-peak [link preview: Mission Peak Trail]" in out
    assert "[WhatsApp item]" in out


def test_group_names_events_and_reactions(env):
    _, index, tools = env
    out = tools.get_messages(wa_thread(index, "Hiking crew"))
    assert "Maya: I can drive" in out
    assert "Jordan: [photo] the view from last time" in out
    assert "Me: who's driving? (Maya reacted ❤️, Jordan reacted 😂)" in out
    assert "Riley: count me in" in out  # name from the message replaces the placeholder
    assert "RETURNED: 4 messages" in out  # the group event is skipped


def test_status_updates_are_not_conversations(env):
    _, index, tools = env
    assert "status text" not in tools.search_messages("status")
    assert index.db.execute("select count(*) from thread where app = 'WhatsApp'").fetchone()[0] == 3


def test_stats_combine_both_apps(env):
    _, _, tools = env
    out = tools.get_relationship_stats(since="2020-01-01", people=["Stephen Lee"])
    assert "iMessage 7 + WhatsApp 5" in out
    assert "t_p" in out and "t_wp" in out


def test_status_lists_whatsapp(env):
    _, _, tools = env
    assert "WhatsApp: 10 messages, 2 1:1 threads, 1 groups" in tools.get_status()


def test_incremental_sync_picks_up_new_whatsapp_message(env):
    wa, index, tools = env
    wa.add(4, 0, "new whatsapp after sync", sender=wa_fixture.MAYA_LID, days_ago=0)
    index.sync()
    assert "new whatsapp after sync" in tools.search_messages("whatsapp after sync")
