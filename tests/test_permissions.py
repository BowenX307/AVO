import unittest
from unittest.mock import patch
from avo import permissions

class PermissionFailureTest(unittest.TestCase):
    def test_failed_checks_do_not_claim_permission(self):
        def broken():
            raise RuntimeError("permission API unavailable")
        with patch.dict(permissions.CHECKS, {k: broken for k in permissions.CHECKS}):
            gaps = permissions.missing()
        self.assertEqual({g["id"] for g in gaps}, {"screen", "accessibility", "input"})
