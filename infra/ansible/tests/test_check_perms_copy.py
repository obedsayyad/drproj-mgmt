"""The bootstrap role ships its own copy of scripts/check-perms.sh (the Ansible tree is rsynced alone to the management VM); the two must not drift."""
import os

from conftest import ROOT

CANONICAL = os.path.join(ROOT, "..", "..", "scripts", "check-perms.sh")
ROLE_COPY = os.path.join(ROOT, "roles", "bootstrap", "files", "check-perms.sh")


def test_role_copy_equals_canonical_script():
    with open(CANONICAL, "rb") as a, open(ROLE_COPY, "rb") as b:
        assert a.read() == b.read()
