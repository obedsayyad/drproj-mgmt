"""Render the policy_version external label of prometheus.yml (stdlib only: also used by tests/compose/promtool.sh).

PromQL cannot turn the value of a metric into a label, so alerts carry the active policy version from Prometheus'
external_labels. Activation re-renders that one value in the live file and reloads Prometheus.
"""
import re
import sys

LABEL = re.compile(r'(external_labels:[ \t]*\n(?:[ \t]+[^\n]*\n)*?[ \t]+policy_version:[ \t]*)"([^"\n]*)"')


def set_policy_version(text, version):
    """Return text with external_labels.policy_version set to `version`; ValueError when the label is not there."""
    if not re.fullmatch(r"[0-9]{1,9}", str(version)):
        raise ValueError("bad_policy_version")
    new, n = LABEL.subn(lambda m: m.group(1) + '"' + str(version) + '"', text, count=1)
    if n != 1:
        raise ValueError("policy_label_not_found")
    return new


def read_policy_version(text):
    m = LABEL.search(text)
    return m.group(2) if m else None


if __name__ == "__main__":  # prom_render.py <in> <version> <out>
    src, version, dst = sys.argv[1:4]
    with open(src) as f, open(dst, "w") as g:
        g.write(set_policy_version(f.read(), version))
