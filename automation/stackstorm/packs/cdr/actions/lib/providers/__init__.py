"""Adapter selection. `provider_adapter` is a required pack setting with no default: a missing or unknown
value is a configuration error, never a silent fallback to simulation or to real."""
from .base import AdapterConfigError, ProviderAdapter, tags_for  # noqa: F401

DEFAULT_SIM_DIR = "/opt/stackstorm/cdr-sim"


def load(config):
    mode = (config or {}).get("provider_adapter")
    if mode == "simulation":
        from .simulation import SimulationAdapter

        return SimulationAdapter(config.get("simulation_state_dir") or DEFAULT_SIM_DIR, config.get("simulation_faults") or None)
    if mode == "real":
        from .real import RealAdapter

        cfg = config.get("real_adapter")
        if not isinstance(cfg, dict):
            raise AdapterConfigError("real_adapter_config_missing")
        return RealAdapter(cfg, ops_url=config.get("supabase_ops_url"))
    raise AdapterConfigError("provider_adapter_missing_or_unknown")
