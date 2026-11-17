import sqlite3
from pathlib import Path
from app.services.mobile_forensic.whatsapp_working_copy_intake import (
    case_id_from_evidence, working_copy_candidates, validate_whatsapp_sqlite,
)


def test_case_id_from_registered_evidence_path():
    assert case_id_from_evidence({}, ["CASE-20261114-ANDROIDPHO_E-400003_FFS.zip/readable_artifacts/whatsapp/Databases/msgstore.db.crypt15"]) == "CASE-20261114-ANDROIDPHO"


def test_working_copy_locator_and_sqlite_validation(tmp_path: Path):
    case = tmp_path / "CASE-20261114-ANDROIDPHO"
    dbpath = case / "03_Working_Copy" / "CASE-20261114-ANDROIDPHO_E-400003_Android_phone_FFS_20261114_094907" / "readable_artifacts" / "whatsapp" / "Databases" / "msgstore.db"
    dbpath.parent.mkdir(parents=True)
    with sqlite3.connect(dbpath) as con:
        con.execute("CREATE TABLE message (id INTEGER PRIMARY KEY, text_data TEXT)")
        con.execute("INSERT INTO message(text_data) VALUES ('sample')")
    assert working_copy_candidates(case) == [dbpath]
    size, tables = validate_whatsapp_sqlite(dbpath)
    assert size > 100
    assert "message" in tables


def test_reject_non_whatsapp_and_symlink(tmp_path: Path):
    case = tmp_path / "CASE-20261114-ANDROIDPHO"
    folder = case / "03_Working_Copy" / "run" / "readable_artifacts" / "whatsapp" / "Databases"
    folder.mkdir(parents=True)
    fake = folder / "msgstore.db"
    fake.write_bytes(b"invalid backup")
    assert working_copy_candidates(case) == [fake]
    try:
        validate_whatsapp_sqlite(fake)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid database accepted")
