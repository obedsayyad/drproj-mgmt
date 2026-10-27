import copy
import json

import pytest
from conftest import load

gen = load("gen_inventory", "scripts/gen-inventory.py")

INPUTS = {"hosts": {
    "management": {"public_ip": "192.0.2.10", "ssh_user": "root", "ssh_key_reference": "~/.ssh/k"},
    "primary": {"public_ip": "192.0.2.11", "ssh_user": "root", "ssh_key_reference": "~/.ssh/k"},
    "load_balancer": {"public_ip": "192.0.2.12", "ssh_user": "root", "ssh_key_reference": "~/.ssh/k"},
    "testing": {"public_ip": "192.0.2.13", "ssh_user": "root", "ssh_key_reference": "~/.ssh/k"},
}}


def test_inputs_become_the_four_groups_plus_an_empty_cloud_app():
    children = gen.build(INPUTS)["all"]["children"]
    assert set(children) == {"management", "primary", "lb", "testing", "fixed", "cloud_app"}
    assert set(children["fixed"]["children"]) == {"management", "primary", "lb", "testing"}
    assert children["lb"]["hosts"]["load_balancer-vm"]["ansible_host"] == "192.0.2.12"
    assert children["cloud_app"]["hosts"] == {}


def test_fixed_hosts_are_operated_as_root_by_default_and_by_the_bootstrap_flag():
    deploy = gen.build(INPUTS)["all"]["children"]["management"]["hosts"]["management-vm"]
    boot = gen.build(INPUTS, bootstrap=True)["all"]["children"]["management"]["hosts"]["management-vm"]
    assert deploy["ansible_user"] == "root" and boot["ansible_user"] == "root"
    assert not deploy["ansible_ssh_private_key_file"].startswith("~")  # expanded


def test_no_host_shares_a_name_with_a_group():
    children = gen.build(INPUTS)["all"]["children"]
    hosts = {h for g in children.values() for h in g.get("hosts", {})}
    assert not hosts & set(children)


@pytest.mark.parametrize("host,field", [("primary", "public_ip"), ("testing", "ssh_key_reference")])
def test_a_missing_value_is_an_error_not_a_partial_inventory(host, field):
    bad = copy.deepcopy(INPUTS)
    bad["hosts"][host][field] = None
    with pytest.raises(gen.InputError, match=f"hosts.{host}.{field}"):
        gen.build(bad)


def test_a_non_ip_and_a_key_reference_url_are_rejected():
    bad = copy.deepcopy(INPUTS)
    bad["hosts"]["primary"]["public_ip"] = "not-an-ip"
    with pytest.raises(gen.InputError):
        gen.build(bad)
    bad = copy.deepcopy(INPUTS)
    bad["hosts"]["primary"]["ssh_key_reference"] = "env://KEY"
    with pytest.raises(gen.InputError, match="path"):
        gen.build(bad)


def test_main_exits_2_and_writes_nothing_on_bad_inputs(tmp_path, capsys):
    bad = copy.deepcopy(INPUTS)
    del bad["hosts"]["load_balancer"]
    src, out = tmp_path / "in.json", tmp_path / "inv.yml"
    src.write_text(json.dumps(bad))
    assert gen.main(["--inputs", str(src), "--output", str(out)]) == 2
    assert not out.exists() and "load_balancer" in capsys.readouterr().err


def test_main_writes_yaml_that_ansible_can_parse(tmp_path):
    yaml = pytest.importorskip("yaml")
    src, out = tmp_path / "in.json", tmp_path / "inv.yml"
    src.write_text(json.dumps(INPUTS))
    assert gen.main(["--inputs", str(src), "--output", str(out), "--bootstrap"]) == 0
    doc = yaml.safe_load(out.read_text())
    assert doc["all"]["children"]["primary"]["hosts"]["primary-vm"]["ansible_host"] == "192.0.2.11"
    assert doc["all"]["children"]["cloud_app"]["hosts"] == {}


def test_controller_key_override_and_local_connection_for_the_controller_itself():
    children = gen.build(INPUTS, ssh_key="/root/.ssh/drproj_mgmt", local_role="management")["all"]["children"]
    mgmt = children["management"]["hosts"]["management-vm"]
    primary = children["primary"]["hosts"]["primary-vm"]
    assert mgmt["ansible_connection"] == "local"
    assert "ansible_connection" not in primary
    assert mgmt["ansible_ssh_private_key_file"] == primary["ansible_ssh_private_key_file"] == "/root/.ssh/drproj_mgmt"
    with pytest.raises(gen.InputError, match="local-role"):
        gen.build(INPUTS, local_role="nope")
