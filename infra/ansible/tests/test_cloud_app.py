import json

from conftest import load

inv = load("cloud_app", "inventory/cloud_app.py")
ROWS = [("11111111-1111-4111-8111-111111111111", "elastic-do-1", "digitalocean", "203.0.113.9/32", "sha256:" + "a" * 64),
        ("22222222-2222-4222-8222-222222222222", "elastic-li-1", "linode", "198.51.100.7", None)]


def run(monkeypatch, capsys, argv, rows=ROWS, env=None):
    monkeypatch.setenv("DR_AUTOMATION_URL", "postgresql://u:secret-pw@db/x")
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(inv, "fetch", lambda url: rows)
    rc = inv.main(argv)
    return rc, capsys.readouterr()


def test_list_returns_the_cloud_app_group_with_hostvars(monkeypatch, capsys):
    rc, out = run(monkeypatch, capsys, ["--list"], env={"CLOUD_VM_SSH_KEY_FILE": "/run/secrets/key"})
    data = json.loads(out.out)
    assert rc == 0 and data["cloud_app"]["hosts"] == sorted(r[0] for r in ROWS)
    hv = data["_meta"]["hostvars"][ROWS[0][0]]
    assert hv["ansible_host"] == "203.0.113.9" and hv["instance_id"] == ROWS[0][0] and hv["provider"] == "digitalocean"
    assert hv["ansible_ssh_private_key_file"] == "/run/secrets/key"
    assert "secret-pw" not in out.out + out.err


def test_host_returns_one_hostvars_or_empty(monkeypatch, capsys):
    rc, out = run(monkeypatch, capsys, ["--host", ROWS[1][0]])
    assert rc == 0 and json.loads(out.out)["slot_key"] == "elastic-li-1"
    rc, out = run(monkeypatch, capsys, ["--host", "nope"])
    assert rc == 0 and json.loads(out.out) == {}


def test_query_excludes_simulated_unbound_and_finished_instances():
    q = inv.QUERY
    assert "NOT i.simulated" in q and "public_ip IS NOT NULL" in q and "lifecycle_state IN" in q
    assert "'deleted'" not in q and "'failed'" not in q


def test_a_database_error_is_a_hard_failure_never_an_empty_inventory(monkeypatch, capsys):
    monkeypatch.setenv("DR_AUTOMATION_URL", "postgresql://u:secret-pw@db/x")

    def boom(url):
        raise RuntimeError("could not connect to postgresql://u:secret-pw@db/x")

    monkeypatch.setattr(inv, "fetch", boom)
    rc = inv.main(["--list"])
    out = capsys.readouterr()
    assert rc == 1 and out.out == "" and "secret-pw" not in out.err and "RuntimeError" in out.err


def test_missing_url_is_a_failure(monkeypatch, capsys):
    monkeypatch.delenv("DR_AUTOMATION_URL", raising=False)
    assert inv.main(["--list"]) == 1
