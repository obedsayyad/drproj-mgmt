"""What a policy change touches (docs/03 activation, docs/07 apply_policy). P4 implements the Prometheus external label and the
capacity limits; sections whose compile step belongs to a later phase are reported, and activation refuses them with
`not_implemented`: a changed HAProxy or k6 setting must never read as applied when nothing applied it."""

# Dotted prefixes. A path belongs to the first group whose prefix it equals or extends.
UNIMPLEMENTED = {
    "prometheus_rules": ("scale_out.cpu_percent", "scale_out.sustained_seconds", "scale_out.cpu_aggregation", "scale_in.cpu_percent",
                         "scale_in.sustained_seconds", "monitoring", "alerts"),  # rule thresholds/windows are hard-coded in monitoring/rules (P6)
    "haproxy": ("health", "failback", "failover.share_cloud_traffic_when_elastic_ready"),  # P6
    "k6": ("demo",),  # P6
    "stackstorm_timers": ("backup.cron", "backup.timezone"),  # P7
}
CAPACITY = ("capacity.max_cloud_vms_per_provider", "capacity.warm_provider", "capacity.warm_instances")


def _under(path, prefix):
    return path == prefix or path.startswith(prefix + ".")


def diff(old, new, prefix=""):
    """Dotted leaf paths whose value differs (lists and scalars compared whole)."""
    out = []
    for k in sorted(set(old) | set(new)):
        p = f"{prefix}.{k}" if prefix else k
        a, b = old.get(k), new.get(k)
        if isinstance(a, dict) and isinstance(b, dict):
            out += diff(a, b, p)
        elif a != b:
            out.append(p)
    return out


def classify(paths):
    unimpl = {g: [p for p in paths if any(_under(p, x) for x in prefixes)] for g, prefixes in UNIMPLEMENTED.items()}
    return {
        "capacity_changed": any(_under(p, c) for p in paths for c in CAPACITY),
        "unimplemented": {g: v for g, v in unimpl.items() if v},
    }


def validate(cfg):
    """The docs/03 bounds this phase relies on (the API validated the full schema when it accepted the change). -> list of error strings."""
    errs = []
    try:
        caps = cfg["capacity"]["max_cloud_vms_per_provider"]
        for p in ("digitalocean", "linode"):
            if not isinstance(caps.get(p), int) or isinstance(caps.get(p), bool) or not 0 <= caps[p] <= 5:
                errs.append(f"capacity.max_cloud_vms_per_provider.{p} must be an integer 0..5")
        warm = cfg["capacity"]["warm_provider"]
        if warm not in ("digitalocean", "linode"):
            errs.append("capacity.warm_provider must be digitalocean or linode")
        elif isinstance(caps.get(warm), int) and caps[warm] < 1:
            errs.append("the warm provider needs a cap of at least 1")
        if cfg["capacity"]["warm_instances"] != 1:
            errs.append("capacity.warm_instances is fixed at 1")
        if cfg["scale_out"]["step_vms"] != 1 or cfg["scale_in"]["step_vms"] != 1:
            errs.append("step_vms must be 1")
        if not cfg["scale_in"]["cpu_percent"] < cfg["scale_out"]["cpu_percent"]:
            errs.append("scale_in.cpu_percent must be lower than scale_out.cpu_percent")
        cps, dl = cfg["provisioning"]["readiness_checkpoints_seconds"], cfg["provisioning"]["readiness_deadline_seconds"]
        if cps != sorted(set(cps)) or not cps or cps[-1] != dl:
            errs.append("readiness checkpoints must increase and end at the deadline")
        co = cfg["coordination"]
        if co["lease_renewal_seconds"] * 3 > co["lease_seconds"]:
            errs.append("lease_renewal_seconds must be at most a third of lease_seconds")
    except (KeyError, TypeError, AttributeError):
        errs.append("the configuration is missing a section this phase needs")
    return errs
