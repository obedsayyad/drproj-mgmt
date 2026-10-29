"""Renders playbooks/app.yml for a cloud_app host and a fixed host with Ansible itself (no host is contacted) and checks the SSH bootstrap vars."""
import json
import os

import pytest

from conftest import ROOT

pytest.importorskip("ansible")
from ansible.inventory.manager import InventoryManager  # noqa: E402
from ansible.parsing.dataloader import DataLoader  # noqa: E402
from ansible.playbook import Playbook  # noqa: E402
from ansible.plugins.loader import init_plugin_loader  # noqa: E402
from ansible.template import Templar  # noqa: E402
from ansible.vars.manager import VariableManager  # noqa: E402

KEY = "/run/secrets/cloud_vm_key"
PUBKEY_TASK = "Read the public half of the approved SSH key on the controller"


@pytest.fixture()
def rendered(tmp_path):
    inv = tmp_path / "inventory"
    inv.mkdir()
    os.symlink(os.path.join(ROOT, "inventory", "group_vars"), inv / "group_vars")
    hosts = {
        "all": {"children": {
            "fixed": {"children": {"primary": {}}},
            "primary": {"hosts": {"primary-vm": {"ansible_host": "192.0.2.10", "ansible_user": "drproj", "ansible_ssh_private_key_file": "/home/x/.ssh/drproj", "drproj_role": "primary"}}},
            "cloud_app": {"hosts": {"i-1": {"ansible_host": "203.0.113.9", "cloud_first_contact_user": "root", "ansible_ssh_private_key_file": KEY}}},
        }}}
    (inv / "hosts.json").write_text(json.dumps(hosts))
    init_plugin_loader()
    loader = DataLoader()
    loader.set_basedir(ROOT)
    manager = InventoryManager(loader=loader, sources=[str(inv / "hosts.json")])
    vm = VariableManager(loader=loader, inventory=manager)
    plays = Playbook.load(os.path.join(ROOT, "playbooks", "app.yml"), variable_manager=vm, loader=loader).get_plays()

    def render(host_name):
        """One entry per play that targets the host: its templar and whether the controller .pub lookup task would run."""
        host = manager.get_host(host_name)
        out = []
        for play in plays:
            if host not in manager.get_hosts(play.hosts):
                continue
            templar = Templar(loader=loader, variables=vm.get_vars(play=play, host=host))
            runs = [task_runs(block, templar, PUBKEY_TASK) for role in play.get_roles() if role.get_name() == "bootstrap" for block in role.compile(play)]
            out.append({"templar": templar, "pubkey_lookup": [r for rs in runs for r in rs]})
        return out

    def runs_of(host_name, task_name):
        host = manager.get_host(host_name)
        out = []
        for play in plays:
            if host in manager.get_hosts(play.hosts):
                templar = Templar(loader=loader, variables=vm.get_vars(play=play, host=host))
                out += [r for role in play.get_roles() if role.get_name() == "bootstrap" for block in role.compile(play)
                        for r in task_runs(block, templar, task_name)]
        return out

    render.task_runs = runs_of
    return render


def enabled(conditions, templar):
    return all(str(templar.template("{{ " + c + " }}")) == "True" for c in conditions or [])


def task_runs(block, templar, name, gate=True):
    """Walk nested blocks; for the named task return whether every enclosing `when` holds."""
    gate = gate and enabled(block.when, templar)
    found = []
    for item in block.block:
        if hasattr(item, "block"):
            found += task_runs(item, templar, name, gate)
        elif item.name == name:
            found.append(gate and enabled(item.when, templar))
    return found


def test_cloud_app_connects_as_the_provisioned_user_then_drproj(rendered):
    plays = rendered("i-1")
    first, second = plays[0], plays[1]
    assert first["templar"].template("{{ ansible_user }}") == "root"
    assert second["templar"].template("{{ ansible_user }}") == "drproj"
    assert not any(second["pubkey_lookup"]), "the deploy play must not run the .pub lookup for cloud_app"


def test_cloud_app_bootstrap_reads_no_pub_file_and_does_not_harden_sshd(rendered):
    first = rendered("i-1")[0]
    t = first["templar"]
    assert str(t.template("{{ bootstrap_harden_sshd }}")) == "False"
    assert str(t.template("{{ bootstrap_authorize_from_connecting_user }}")) == "True"
    assert first["pubkey_lookup"] == [False], "the controller .pub lookup must be skipped for cloud_app"


def test_fixed_host_keeps_the_pub_lookup_and_stays_root_without_hardening(rendered):
    plays = rendered("primary-vm")
    assert len(plays) == 1
    t = plays[0]["templar"]
    assert str(t.template("{{ bootstrap_harden_sshd }}")) == "False"
    assert str(t.template("{{ bootstrap_disable_password_auth }}")) == "False"
    assert str(t.template("{{ bootstrap_authorize_from_connecting_user }}")) == "False"
    assert plays[0]["pubkey_lookup"] == [True], "fixed hosts install the controller-side .pub"


def test_root_mode_skips_user_and_sudo_creation_and_manages_root_ssh_only(rendered):
    t = rendered("primary-vm")[0]["templar"]
    assert t.template("{{ bootstrap_deploy_user }}") == "root"
    assert t.template("{{ bootstrap_deploy_home }}") == "/root"
    for task in ("Create the deploy user", "Allow the deploy user passwordless sudo"):
        assert not any(rendered.task_runs("primary-vm", task)), f"{task} must be skipped when the deploy user is root"
    assert rendered.task_runs("primary-vm", PUBKEY_TASK) == [True]


def test_cloud_vm_still_creates_the_drproj_user(rendered):
    t = rendered("i-1")[0]["templar"]
    assert t.template("{{ bootstrap_deploy_user }}") == "drproj"
    assert t.template("{{ bootstrap_deploy_home }}") == "/home/drproj"
    assert all(rendered.task_runs("i-1", "Create the deploy user")[:1])


DOCKER_INPUTS = {"versions": {"docker": "29.5.2"}}


@pytest.mark.parametrize("distribution,release,base", [
    ("Debian", "bookworm", "https://download.docker.com/linux/debian"),
    ("Debian", "trixie", "https://download.docker.com/linux/debian"),
    ("Ubuntu", "jammy", "https://download.docker.com/linux/ubuntu"),
])
def test_docker_repository_follows_the_distribution(rendered, distribution, release, base):
    t = rendered("primary-vm")[0]["templar"]
    t.available_variables = {**t.available_variables, "inputs": DOCKER_INPUTS, "ansible_facts": {
        "distribution": distribution, "distribution_release": release, "architecture": "x86_64"}}
    assert t.template("{{ bootstrap_docker_repo_base }}") == base
    assert t.template("{{ bootstrap_docker_pin }}") == "5:29.5.2-*"
    assert t.template("{{ bootstrap_docker_repo_base }}/gpg").startswith(base)


def test_docker_apt_suite_line_uses_distribution_release():
    import yaml
    tasks = yaml.safe_load(open(os.path.join(ROOT, "roles/bootstrap/tasks/main.yml")))
    repo = next(t for t in tasks if t["name"] == "Add the Docker apt repository")["ansible.builtin.apt_repository"]["repo"]
    assert "{{ ansible_facts['distribution_release'] }} stable" in repo and "bookworm" not in repo and "trixie" not in repo
    check = next(t for t in tasks if t["name"] == "Check the inputs this role needs")["ansible.builtin.assert"]["that"]
    guard = next(c for c in check if "distribution_major_version" in c)
    assert "in ['12', '13']" in guard
