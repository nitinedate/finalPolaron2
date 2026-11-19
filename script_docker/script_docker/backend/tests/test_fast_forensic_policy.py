"""Fast Forensic keeps named databases and skips bulk copies that have no parser."""
from types import SimpleNamespace

from app.services.fast_forensic_policy import metadata_only_bundle, metadata_only_path

_HASH = (
    "CASE.zip/02_Original_Extraction/ios_image/00008150-0002659C0CBB401C/"
    "5f/5fef3f698501c4aa11bb22cc33dd44ee55ff6677"
)


def test_ios_backup_hash_blobs_stay_indexed():
    assert metadata_only_path(_HASH, "") is True
    assert metadata_only_bundle([SimpleNamespace(path=_HASH, extension="")]) is True


def test_named_databases_and_plists_stay_on_native_parsers():
    named = [
        "CASE.zip/readable_artifacts/whatsapp/7c7fba66680ef796_ChatStorage.sqlite",
        "CASE.zip/readable_artifacts/sms_imessage/3d0d7e5fb2ce2888_sms.db",
        "CASE.zip/02_Original_Extraction/ios_image/UDID/Manifest.db",
        "CASE.zip/02_Original_Extraction/ios_image/UDID/Info.plist",
    ]
    for path in named:
        assert metadata_only_path(path) is False


def test_thumbnails_and_photos_stay_indexed():
    thumb = "CASE.zip/readable_artifacts/whatsapp/000200763eedb2eb_a62b667a.thumb"
    assert metadata_only_path(thumb, ".thumb") is True
    assert metadata_only_path("CASE.zip/media/photo.jpg", ".jpg") is True


def test_database_companion_keeps_the_bundle_on_the_native_parser():
    bundle = [
        SimpleNamespace(path=_HASH, extension=""),
        SimpleNamespace(path="CASE.zip/readable_artifacts/sms.db", extension=".db"),
    ]
    assert metadata_only_bundle(bundle) is False
