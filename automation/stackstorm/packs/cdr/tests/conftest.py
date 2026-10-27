"""Runs the actions without a StackStorm install: if st2common is absent, a minimal stand-in for
st2common.runners.base_action.Action is registered (a test double, not StackStorm)."""
import os
import sys
import types

HERE = os.path.dirname(__file__)
for sub in ("actions", os.path.join("actions", "lib")):  # st2 puts actions/lib on the path
    sys.path.insert(0, os.path.join(HERE, "..", sub))

try:
    import st2common.runners.base_action  # noqa: F401
except ImportError:
    pkg = types.ModuleType("st2common")
    runners = types.ModuleType("st2common.runners")
    base = types.ModuleType("st2common.runners.base_action")

    class Action:  # noqa: D101
        def __init__(self, config=None):
            self.config = config or {}

    base.Action = Action
    sys.modules.update({"st2common": pkg, "st2common.runners": runners, "st2common.runners.base_action": base})
