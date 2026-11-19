"""Linked-id chats use the saved address-book name instead of the raw JID."""

from __future__ import annotations

import sqlite3

from app.services.artifact_group_browse import chat_label_sort_key
from app.services.mobile_forensic.whatsapp_names import (
    jid_display_names,
    parse_android_contacts_dump,
)
from app.services.whatsapp_evidence_browse import _row


DUMP = """
Row: 0 display_name=Ada Lovelace, data1=+15551230000, mimetype=vnd.android.cursor.item/phone_v2
Row: 1 display_name=School Group, data1=not-a-phone, mimetype=vnd.android.cursor.item/name
""".strip()


def _store() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
        CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INT, subject TEXT);
        CREATE TABLE jid_map (lid_row_id INT, jid_row_id INT);
        CREATE TABLE lid_display_name (lid_row_id INT, display_name TEXT, username TEXT);
        INSERT INTO jid VALUES (1, '15551230000', 's.whatsapp.net', '15551230000@s.whatsapp.net');
        INSERT INTO jid VALUES (2, '158100000000001', 'lid', '158100000000001@lid');
        INSERT INTO jid VALUES (3, '158100000000002', 'lid', '158100000000002@lid');
        INSERT INTO jid VALUES (4, '120363', 'g.us', '120363@g.us');
        INSERT INTO chat VALUES (1, 2, NULL);
        INSERT INTO chat VALUES (2, 3, NULL);
        INSERT INTO chat VALUES (3, 4, 'School Group');
        INSERT INTO jid_map VALUES (2, 1);
        """
    )
    return conn


def test_lid_chat_uses_address_book_name_and_leaves_unknown_lids_alone():
    names = jid_display_names(_store(), parse_android_contacts_dump(DUMP))
    assert names["158100000000001@lid"] == "Ada Lovelace"
    assert names["15551230000@s.whatsapp.net"] == "Ada Lovelace"
    assert "158100000000002@lid" not in names
    assert names["120363@g.us"] == "School Group"


def test_chat_list_puts_letters_before_numbers():
    titles = ["1011@lid", "0@s.whatsapp.net", "bob", "Ada", "Zed"]
    ordered = sorted(titles, key=chat_label_sort_key)
    assert ordered == ["Ada", "bob", "Zed", "0@s.whatsapp.net", "1011@lid"]


def test_people_list_title_is_the_saved_name():
    record = {
        "artifact_id": "a1",
        "state": "allocated",
        "data": {
            "application": "whatsapp",
            "conversation_id": "158100000000001@lid",
            "conversation_name": "158100000000001@lid",
            "sender": "158100000000001@lid",
            "body": "hello",
            "from_me": 0,
        },
        "forensic": {},
    }
    row = _row(record, "job", {"158100000000001@lid": "Ada Lovelace"})
    assert row["title"] == "Ada Lovelace"
    assert row["metadata"]["person_name"] == "Ada Lovelace"
    assert row["metadata"]["sender_display_name"] == "Ada Lovelace"
    assert row["metadata"]["conversation_id"] == "158100000000001@lid"
