"""The lb role vendors the Compose project and HAProxy files from drproj-app/deploy/compose/lb (the source of truth), like the app role."""
import os

from conftest import ROOT

SRC = os.path.abspath(os.path.join(ROOT, "..", "..", "..", "drproj-app", "deploy", "compose", "lb"))
FILES = {"compose.yaml": "compose.yaml", "haproxy/haproxy.cfg": "haproxy/haproxy.cfg", "haproxy/errors/503.http": "haproxy/errors/503.http"}


def test_vendored_lb_files_match_the_source():
    for vendored, source in FILES.items():
        assert open(os.path.join(ROOT, "roles", "lb", "files", vendored)).read() == open(os.path.join(SRC, source)).read(), vendored


def test_env_template_sets_every_name_the_compose_project_reads():
    import re

    names = lambda path: {m.group(1) for line in open(path) if (m := re.match(r"^([A-Z][A-Z0-9_]*)=", line))}
    template = names(os.path.join(ROOT, "roles", "lb", "templates", "env.j2"))
    example = names(os.path.join(SRC, ".env.example"))
    assert example == template, f"differs: {sorted(example ^ template)}"
