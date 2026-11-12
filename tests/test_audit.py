import tempfile
import unittest
from pathlib import Path
from crypto_observer.core import Store
from crypto_observer.audit import audit,review_evidence

class AuditTests(unittest.TestCase):
 def test_no_data_cannot_complete(self):
  s=Store(':memory:')
  try:
   a=audit(s)
   self.assertEqual(a['completion'],'not_complete')
   self.assertIn('matured_7_30_90_day_outcomes',a['remaining_requirements'])
   self.assertFalse(a['case_review']['complete'])
  finally:s.close()
 def test_missing_artifacts_not_passed(self):
  s=Store(':memory:')
  try:
   with tempfile.TemporaryDirectory() as folder:
    self.assertFalse(review_evidence(s,Path(folder))['complete'])
  finally:s.close()
