import json
import unittest
from billing_readmodel import _due_date_source

class TestRepaymentSource(unittest.TestCase):
    def test_extracted_date_must_match_column(self):
        raw=json.dumps({'meta':{'due_date':{'source':'extracted','value':'2026-10-28'}}})
        self.assertEqual(_due_date_source(raw,'2026-10-28'),'extracted')
        self.assertIsNone(_due_date_source(raw,'2026-10-29'))

    def test_missing_or_malformed_evidence_is_not_verified(self):
        for raw in (None,'{}','[]','null','bad'):
            self.assertIsNone(_due_date_source(raw,'2026-10-28'))

    def test_inferred_date_stays_inferred(self):
        raw=json.dumps({'meta':{'due_date':{'source':'inferred','value':'2026-10-28'}}})
        self.assertEqual(_due_date_source(raw,'2026-10-28'),'inferred')
