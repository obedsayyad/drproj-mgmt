"""Helpers shared by the P4.2 provisioning actions."""
import functools
import json

from cdr_db import call
from haproxy_admin import AdminError
from lease_keeper import LeaseLost
from prom_target import PolicyCompileConfigError
from providers import AdapterConfigError


def guarded(fn):
    """Map the failures every provisioning action shares to short codes (never exception text)."""

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except LeaseLost:
            return False, "lease_lost"
        except AdapterConfigError:
            return False, "provider_adapter_missing_or_unknown"
        except PolicyCompileConfigError as exc:
            return False, str(exc)
        except NotImplementedError:
            return False, "not_implemented"
        except AdminError as exc:  # short code only (admin_unreachable, command_refused, effect_unverified, ...)
            return False, str(exc)

    return wrapper


def advance(config, operation_id, event_type, step, status=None, message=None, outcome=None, source="automation"):
    """ops.advance_operation (the one writer of status/step/events). Returns (ok, sequence-or-code)."""
    ok, rows = call(
        config,
        "SELECT ops.advance_operation(%s::uuid, %s, %s, %s, %s, %s::jsonb, %s)",
        (operation_id, status, step, event_type, message, None if outcome is None else json.dumps(outcome), source),
    )
    return (True, rows[0][0]) if ok else (False, rows)


def policy_section(config, operation_id, section):
    """The `section` object of the policy version the operation captured."""
    ok, rows = call(
        config,
        "SELECT p.configuration -> %s FROM ops.policy_versions p JOIN ops.operations o ON o.policy_version = p.version WHERE o.id = %s::uuid",
        (section, operation_id),
    )
    if not ok:
        return False, rows
    if not rows or rows[0][0] is None:
        return False, "policy_not_found"
    return True, rows[0][0]


# Failure codes a workflow names on purpose; recorded as the outcome error instead of the raw task result.
KNOWN_CODES = ("not_implemented", "policy_invalid", "reload_failed", "verify_failed", "template_missing", "lease_unavailable", "held_uncertain", "failback_regressed")


def failure_code(text, default="workflow_error"):
    return next((c for c in KNOWN_CODES if c in str(text)), default)


def policy_versions(config, operation_id):
    """The apply_policy operation's pending version and the active one: (True, {version, from_version, pending, active}) or (False, code)."""
    ok, rows = call(config, "SELECT (parameters ->> 'policy_version')::integer FROM ops.operations WHERE id = %s::uuid AND kind = 'apply_policy'", (operation_id,))
    if not ok:
        return False, rows
    if not rows or rows[0][0] is None:
        return False, "not_an_apply_policy_operation"
    version = rows[0][0]
    ok, rows = call(config, "SELECT version, status, configuration FROM ops.policy_versions WHERE version = %s OR status = 'active'", (version,))
    if not ok:
        return False, rows
    by = {r[0]: r for r in rows}
    pending = by.get(version)
    active = next((r for r in rows if r[1] == "active"), None)
    if pending is None or active is None:
        return False, "policy_not_found"
    return True, {"version": version, "from_version": active[0], "pending_status": pending[1], "pending": pending[2], "active": active[2]}
