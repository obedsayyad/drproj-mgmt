"""Start capacity executions through the StackStorm API (reconcile resumes of interrupted operations). Dispatching is
asynchronous; each entry reports the new execution id or a short code."""
from st2_api import St2Api, St2Unavailable
from st2common.runners.base_action import Action


class Dispatch(Action):
    def run(self, runs, api=None):
        out = []
        try:
            api = api or St2Api()
        except St2Unavailable as exc:
            return False, str(exc)
        for r in runs:
            try:
                out.append({"ref": r["ref"], "operation_id": r["parameters"].get("operation_id"), "execution_id": api.run(r["ref"], r["parameters"])})
            except St2Unavailable as exc:
                out.append({"ref": r["ref"], "operation_id": r["parameters"].get("operation_id"), "error": str(exc)})
        return True, {"dispatched": out}
