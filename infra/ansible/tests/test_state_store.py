"""The lb role loads HAProxy's server-state from the latest VALID dump of the membership store (docs/08): every be_app server line of
haproxy.cfg must be present. validate_state.py is standalone (it runs on management); its expectations come from haproxy.cfg itself."""
import importlib.util
import os
import re

import yaml

from conftest import ROOT

HEADER = "1\n# be_id be_name srv_id srv_name srv_addr srv_op_state srv_admin_state srv_uweight srv_iweight srv_time_since_last_change srv_check_status srv_check_result srv_check_health srv_check_state srv_agent_state bk_f_forced_id srv_f_forced_id srv_fqdn srv_port srvrecord srv_use_ssl srv_check_port srv_check_addr srv_agent_addr srv_agent_port\n"


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vs = load("validate_state", "roles/lb/files/validate_state.py")


def dump(names, backend="be_app"):
    return HEADER + "".join(f"3 {backend} {i} {n} 10.0.0.{i} 2 0 100 100 5 0 0 0 0 0 0 0 - 8443 - 1 0 - - 0\n" for i, n in enumerate(names, 1))


def expected_from_cfg():
    cfg = open(os.path.join(ROOT, "roles", "lb", "files", "haproxy", "haproxy.cfg")).read()
    block = re.search(r"(?sm)^backend be_app.*?(?=\n(?:backend|frontend|listen|defaults|global) |\Z)", cfg).group(0)
    return re.findall(r'(?m)^\s+server\s+"?([A-Za-z0-9_-]+)"?\s', block)


def test_the_role_default_extracts_exactly_the_be_app_server_lines():
    expr = yaml.safe_load(open(os.path.join(ROOT, "roles", "lb", "defaults", "main.yml")))["lb_expected_servers"]
    assert "regex_search('(?sm)^backend be_app" in expr  # anchored: `default_backend be_app` must not match
    names = expected_from_cfg()
    assert names[0] == "primary" and len(names) == 15 and "acme" not in names and "cli" not in names


def test_a_dump_is_valid_only_when_every_expected_line_is_present(tmp_path):
    names = expected_from_cfg()
    (tmp_path / "20260101T000000000000Z.state").write_text(dump(names))
    (tmp_path / "20260102T000000000000Z.state").write_text(dump(names[:-1]))          # newer but a line is missing
    (tmp_path / "20260103T000000000000Z.state").write_text("garbage")                  # newest but not a dump
    (tmp_path / "20260104T000000000000Z.state").write_text(dump(names, backend="x"))   # other backend only
    assert vs.newest_valid(str(tmp_path), names) == "20260101T000000000000Z.state"
    assert vs.newest_valid(str(tmp_path), names + ["elastic-do-6"]) is None
    assert vs.newest_valid(str(tmp_path / "missing"), names) is None


def test_cli_prints_the_name_and_fails_when_nothing_validates(tmp_path, capsys):
    names = expected_from_cfg()
    assert vs.main(["--dir", str(tmp_path), "--expected", ",".join(names)]) == 1
    (tmp_path / "20260101T000000000000Z.state").write_text(dump(names))
    assert vs.main(["--dir", str(tmp_path), "--expected", ",".join(names)]) == 0
    assert capsys.readouterr().out.strip() == "20260101T000000000000Z.state"


def test_the_python_pack_store_and_the_ansible_script_agree_on_validity(tmp_path):
    import sys

    sys.path.insert(0, os.path.join(ROOT, "..", "..", "automation", "stackstorm", "packs", "cdr", "actions", "lib"))
    import haproxy_admin as hx

    names = expected_from_cfg()
    for text in (dump(names), dump(names[:-1]), "garbage", HEADER, dump(names, backend="x")):
        assert vs.valid(text, names) == (names and _pack_valid(hx, text, names))


def _pack_valid(hx, text, names):
    try:
        return set(names) <= set(hx.parse_state(text).servers)
    except hx.AdminError:
        return False
