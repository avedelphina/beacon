import pytest

from backend.tiers import CAPABILITY_TIERS, Tier, requires_confirm, tier_for


@pytest.mark.parametrize("capability", list(CAPABILITY_TIERS))
def test_every_registered_capability_resolves(capability):
    tier, rationale = tier_for(capability)
    assert isinstance(tier, Tier)
    assert rationale  # every assignment carries its own reasoning


def test_unknown_capability_raises():
    with pytest.raises(KeyError):
        tier_for("delete_everything")


def test_read_only_capabilities_never_need_confirm():
    for capability in ("status", "logs", "list_plugins", "reconcile_check", "config_diff"):
        tier, _ = tier_for(capability)
        assert tier < Tier.T2
        assert not requires_confirm(tier)


def test_t2_and_above_require_confirm():
    for capability in ("restart", "apply_fix", "push_config", "update_plugin", "deploy", "update_agent", "decommission"):
        tier, _ = tier_for(capability)
        assert tier >= Tier.T2
        assert requires_confirm(tier)


def test_decommission_escalates_only_on_purge_or_remove_user():
    plain, _ = tier_for("decommission")
    purge, _ = tier_for("decommission", purge=True)
    remove_user, _ = tier_for("decommission", remove_user=True)
    both, _ = tier_for("decommission", purge=True, remove_user=True)

    assert plain == Tier.T4
    assert purge == Tier.T5
    assert remove_user == Tier.T5
    assert both == Tier.T5


def test_decommission_false_flags_do_not_escalate():
    tier, _ = tier_for("decommission", purge=False, remove_user=False)
    assert tier == Tier.T4
