"""Placeholder for a step that a later phase implements (docs/11). It performs nothing and ALWAYS fails, with a short code naming the step:
a skeleton workflow that is triggered by accident must end `failed` / `not_implemented`, never look like it worked."""
from st2common.runners.base_action import Action


class NotImplementedStep(Action):
    def run(self, step, phase):
        return False, f"not_implemented:{step}:{phase}"
