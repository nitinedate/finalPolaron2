import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.services.fast_forensic_policy import metadata_only_bundle,fast_forensic_enabled
@dataclass
class Item:
    path:str
    extension:str=''

class FastPolicyTests(unittest.TestCase):
    def test_bulk_media(self):
        self.assertTrue(metadata_only_bundle([Item('DCIM/100APPLE/IMG_0001.HEIC')]))
    def test_documents_indexed(self):
        self.assertTrue(metadata_only_bundle([Item('Documents/notes.pdf')]))
    def test_unknown_retained(self):
        self.assertFalse(metadata_only_bundle([Item('Library/System/opaque.blob')]))
    def test_sqlite_retained(self):
        self.assertFalse(metadata_only_bundle([Item('AppDomain/ChatStorage.sqlite')]))
    def test_sqlite_sidecar_retained(self):
        self.assertFalse(metadata_only_bundle([Item('foo.db'),Item('foo.db-wal')]))
    def test_deleted_media_retained(self):
        self.assertFalse(metadata_only_bundle([Item('Recovered/Deleted/file.jpg')]))
    def test_mixed_bundle_retained(self):
        self.assertFalse(metadata_only_bundle([Item('Documents/a.pdf'),Item('Database/contacts.db')]))
    def test_default_mode(self):
        from unittest.mock import patch
        with patch.dict('os.environ', {}, clear=True):
            self.assertTrue(fast_forensic_enabled())
        with patch.dict('os.environ', {'FORENSIC_PROCESSING_MODE':'deep_forensic'}):
            self.assertFalse(fast_forensic_enabled())
if __name__=='__main__': unittest.main()
