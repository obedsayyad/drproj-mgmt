"""The app role's .env template must set exactly the names the relief app's .env.example declares (drproj-app), minus the one secret the VM does not need."""
import os
import re

from conftest import ROOT

EXAMPLE = os.path.abspath(os.path.join(ROOT, "..", "..", "..", "drproj-app", "deploy", "compose", "app", ".env.example"))
NOT_ON_VM = {"NODE_EXPORTER_BASIC_AUTH_PASSWORD"}  # only its bcrypt hash reaches the VM (bootstrap role); the relief container must not see the password


def names(path):
    return {m.group(1) for line in open(path) if (m := re.match(r"^([A-Z][A-Z0-9_]*)=", line))}


def test_env_template_matches_the_app_env_example():
    template = names(os.path.join(ROOT, "roles", "app", "templates", "env.j2"))
    example = names(EXAMPLE)
    assert example - NOT_ON_VM <= template, f"missing from env.j2: {sorted(example - NOT_ON_VM - template)}"
    assert template - example <= {"NODE_EXPORTER_TAG"}, f"unknown names in env.j2: {sorted(template - example)}"


def test_vendored_compose_matches_the_source():
    src = os.path.abspath(os.path.join(ROOT, "..", "..", "..", "drproj-app", "deploy", "compose", "app", "compose.yaml"))
    vendored = open(os.path.join(ROOT, "roles", "app", "templates", "compose.yaml.j2")).read().split("\n", 1)[1]
    assert vendored == open(src).read()
