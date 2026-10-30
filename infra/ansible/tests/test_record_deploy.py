import json

import pytest
import yaml
from conftest import ROOT, load

rd = load("record_deploy", "roles/app/files/record_deploy.py")
DIGEST = "sha256:" + "a" * 64
UUID = "11111111-2222-3333-4444-555555555555"


class FakeConn:
    """Stands in for psycopg: records every statement and answers the fixed-host lookup."""

    def __init__(self, primary_id=UUID, fail=None):
        self.calls, self.primary_id, self.fail = [], primary_id, fail

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if self.fail and "record_instance_deploy" in sql:
            raise self.fail
        return self

    def fetchone(self):
        return None if self.primary_id is None else (self.primary_id,)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_cloud_instance_id_is_passed_through_without_a_lookup():
    c = FakeConn()
    assert rd.record(c, UUID, DIGEST, "v1", "sha256:b") == UUID
    assert len(c.calls) == 1 and "record_instance_deploy" in c.calls[0][0]
    assert c.calls[0][1] == (UUID, DIGEST, "v1", "sha256:b")


def test_fixed_host_is_resolved_by_provider_resource_id():
    c = FakeConn()
    assert rd.record(c, "primary", DIGEST, "v1", "r") == UUID
    assert c.calls[0][1] == ("primary",) and "self_hosted" in c.calls[0][0]
    assert c.calls[1][1][0] == UUID


def test_unknown_fixed_host_fails_without_calling_the_function():
    c = FakeConn(primary_id=None)
    with pytest.raises(LookupError):
        rd.record(c, "primary", DIGEST, "v1", "r")
    assert len(c.calls) == 1


def test_main_success_prints_json_and_never_the_url(capsys):
    env = {"DR_AUTOMATION_URL": "postgres://u:secret@h/db"}
    assert rd.main(["primary", DIGEST, "v1", "r"], connect_fn=lambda url: FakeConn(), environ=env) == 0
    out = capsys.readouterr()
    assert json.loads(out.out) == {"recorded": True, "instance_id": UUID, "image_digest": DIGEST}
    assert "secret" not in out.out + out.err


def test_main_failure_is_nonzero_and_missing_url_is_an_error(capsys):
    env = {"DR_AUTOMATION_URL": "postgres://u:secret@h/db"}
    assert rd.main([UUID, DIGEST, "v", "r"], connect_fn=lambda url: FakeConn(fail=RuntimeError("bad_argument")), environ=env) == 1
    assert "secret" not in capsys.readouterr().err
    assert rd.main([UUID, DIGEST, "v", "r"], environ={}) == 1


def test_role_records_after_readiness_for_every_host_kind():
    tasks = yaml.safe_load(open(f"{ROOT}/roles/app/tasks/main.yml"))
    names = [t["name"] for t in tasks]
    i = names.index("Record the deploy in ops.instances (ops.record_instance_deploy)")
    assert i > names.index("Wait for readiness") and i > names.index("Compare the running image with the pinned digest")
    t = tasks[i]
    assert t["delegate_to"] == "localhost" and "hosts" not in t  # no group filter: primary and cloud_app alike
    assert "RepoDigests" in json.dumps(t["ansible.builtin.command"]["argv"])
