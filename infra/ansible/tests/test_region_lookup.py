import sys

from conftest import load

rl = load("region_lookup", "roles/app/files/region_lookup.py")


def test_label_is_city_comma_country():
    assert rl.build_label({"ip": "1.2.3.4", "city": "Pune", "region": "Maharashtra", "country": "IN"}) == "Pune, IN"


def test_incomplete_or_odd_answers_give_no_label():
    for bad in ({}, {"city": "Pune"}, {"country": "IN"}, {"city": "<b>x</b>", "country": "IN"}, {"bogon": True}, None, "x"):
        assert rl.build_label(bad) == ""


def test_lookup_failure_falls_back_to_empty_not_a_guess():
    def down(ip):
        raise OSError("no route")

    assert rl.lookup("103.141.79.171", fetch=down) == ""
    assert rl.lookup("103.141.79.171", fetch=lambda ip: {"error": {"title": "rate limited"}}) == ""
    assert rl.lookup("not-an-ip") == ""


def test_lookup_uses_the_given_ip():
    seen = []
    assert rl.lookup("103.141.79.171", fetch=lambda ip: seen.append(ip) or {"city": "Pune", "country": "IN"}) == "Pune, IN"
    assert seen == ["103.141.79.171"]


def test_script_fallback_end_to_end_with_unreachable_network(monkeypatch):
    import urllib.request

    def boom(*a, **k):
        raise OSError("offline")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert rl.lookup("103.141.79.171") == ""
