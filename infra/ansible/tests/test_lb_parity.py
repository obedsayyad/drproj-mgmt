"""The lb role vendors the Compose project and HAProxy files from drproj-app/deploy/compose/lb (the source of truth), like the app role."""
import os

from conftest import ROOT

SRC = os.path.abspath(os.path.join(ROOT, "..", "..", "..", "drproj-app", "deploy", "compose", "lb"))
FILES = {"compose.yaml": "compose.yaml", "haproxy/haproxy.cfg": "haproxy/haproxy.cfg", "haproxy/errors/503.http": "haproxy/errors/503.http", "haproxy/admin_allow.regex": "haproxy/admin_allow.regex", "check_config.py": "check_config.py"}


def test_vendored_lb_files_match_the_source():
    for vendored, source in FILES.items():
        assert open(os.path.join(ROOT, "roles", "lb", "files", vendored)).read() == open(os.path.join(SRC, source)).read(), vendored


def test_env_template_sets_every_name_the_compose_project_reads():
    import re

    names = lambda path: {m.group(1) for line in open(path) if (m := re.match(r"^([A-Z][A-Z0-9_]*)=", line))}
    template = names(os.path.join(ROOT, "roles", "lb", "templates", "env.j2"))
    example = names(os.path.join(SRC, ".env.example"))
    assert example == template, f"differs: {sorted(example ^ template)}"


def test_policy_check_accepts_the_vendored_config_and_rejects_violations(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("check_config", os.path.join(ROOT, "roles", "lb", "files", "check_config.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.problems(open(os.path.join(ROOT, "roles", "lb", "files", "haproxy", "haproxy.cfg")).read()) == []
    assert mod.problems("global\n    stats socket ipv4@:9000 level admin\n")
    assert mod.problems("frontend a\n    bind :9999 ssl crt x ca-file y\n")
    assert mod.problems("frontend a\n    bind :9999 ssl crt x ca-file y verify optional\n")


def test_addr_is_not_allowed_on_primary():
    import re

    lines = [l for l in open(os.path.join(ROOT, "roles", "lb", "files", "haproxy", "admin_allow.regex")).read().split("\n") if l]
    ok = lambda cmd: any(re.search(l, cmd) for l in lines)
    assert not ok("set server be_app/primary addr 10.0.0.1 port 8443\n")
    assert ok("set server be_app/iwarm_act addr 10.0.0.1 port 8443\n")
    assert ok("set server be_app/elastic-linode-5 addr 10.0.0.1 port 8443\n")
    assert ok("set server be_app/primary state maint\n") and ok("set server be_app/primary weight 50\n")
