"""CLI-created declarative filters reach the existing webhook engine (#144398)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli._parser import build_top_level_parser
from hermes_cli.subcommands.webhook import build_webhook_parser
from hermes_cli.webhook import _load_subscriptions, _mutate_subscriptions, webhook_command


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    (root / "config.yaml").write_text(
        "platforms:\n  webhook:\n    enabled: true\n", encoding="utf-8"
    )
    return root


def _parse(*argv):
    parser, subparsers, _ = build_top_level_parser()
    build_webhook_parser(subparsers, cmd_webhook=webhook_command)
    return parser.parse_args(["webhook", *argv])


def _run(*argv):
    args = _parse(*argv)
    return args.func(args)


@pytest.mark.parametrize("file_input", [False, True])
def test_filters_round_trip_to_runtime(home, tmp_path, file_input):
    from gateway.platforms.webhook_filters import WebhookRouteProcessor

    filters = [
        {"field": "action", "equals": "closed"},
        {"any": [
            {"field": "pull_request.merged", "equals": True},
            {"not": {"field": "pull_request.draft", "exists": True}},
        ]},
    ]
    raw = json.dumps(filters)
    if file_input:
        source = tmp_path / "filters.json"
        source.write_text(raw, encoding="utf-8-sig")
        option = ["--filters-file", str(source)]
    else:
        option = ["--filters", raw]
    assert _run("subscribe", "merged", "--events", "pull_request", *option) is None
    route = _load_subscriptions()["merged"]
    assert route["filters"] == filters
    processor = WebhookRouteProcessor()
    assert processor.route_filters_match(route, {
        "action": "closed", "pull_request": {"merged": True, "draft": False},
    }, "pull_request", {})
    assert not processor.route_filters_match(route, {
        "action": "opened", "pull_request": {"merged": True},
    }, "pull_request", {})


def test_updates_preserve_filters_profile_secret_and_disabled_state(home, capsys):
    profile = home / "profiles" / "worker"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("{}\n", encoding="utf-8")
    filters = [{"field": "label", "equals": "private-filter-value"}]
    _run("subscribe", "alerts", "--route-profile", "worker", "--filters", json.dumps(filters))
    original = _load_subscriptions()["alerts"]
    _mutate_subscriptions(lambda routes: routes["alerts"].update(enabled=False))
    capsys.readouterr()
    _run("subscribe", "alerts", "--description", "updated")
    updated = _load_subscriptions()["alerts"]
    assert updated["filters"] == filters
    assert updated["profile"] == "worker"
    assert updated["secret"] == original["secret"]
    assert updated["enabled"] is False
    _run("list")
    output = capsys.readouterr().out
    assert "Filters:" in output
    assert "private-filter-value" not in output
    _run("subscribe", "alerts", "--filters", "[]")
    assert _load_subscriptions()["alerts"]["filters"] == []
    assert _load_subscriptions()["alerts"]["enabled"] is False


def test_default_subscription_has_no_filter(home, capsys):
    assert _run("subscribe", "all-events") is None
    assert "filters" not in _load_subscriptions()["all-events"]
    _run("list")
    assert "Filters:" not in capsys.readouterr().out


@pytest.mark.parametrize("raw", [
    "private-filter-value", "null", "true", "[1]", "{}",
    '{"field":"x","equal":"private-filter-value"}',
    '{"field":"x","equals":1,"typo":"private-filter-value"}',
    '{"field":"x"}', '{"field":" ","equals":1}',
    '{"field":"x","exists":"false"}', '{"field":"x","missing":false}',
    '{"field":"x","in":"private-filter-value"}',
    '{"field":"x","in_file":""}', '{"field":"x","regex":"["}',
    '{"field":"x","equals":NaN}', '{"any":{}}', '{"not":null}',
    '{"field":"x","regex":"a{100000000000000000000}"}',
    json.dumps({"field": "x", "regex": "(" * 1000}),
    '{"field":"x","in_file":[]}', '{"field":"x","regex":42}',
    " " * 100_000 + "[]",
    '{"all":[],"field":"x","equals":1}',
    '{"field":"x","equals":1,"not_equals":2}',
])
def test_invalid_filters_do_not_write_or_echo_input(home, capsys, raw):
    _run("subscribe", "alerts", "--description", "original")
    before = (home / "webhook_subscriptions.json").read_bytes()
    capsys.readouterr()
    assert _run("subscribe", "alerts", "--filters", raw) == 1
    assert (home / "webhook_subscriptions.json").read_bytes() == before
    output = capsys.readouterr().out
    assert "Error:" in output
    assert "private-filter-value" not in output


def test_input_sources_are_mutually_exclusive(home, tmp_path):
    with pytest.raises(SystemExit) as error:
        _parse("subscribe", "alerts", "--filters", "[]", "--filters-file", str(tmp_path / "x"))
    assert error.value.code == 2
    assert not (home / "webhook_subscriptions.json").exists()


@pytest.mark.parametrize("kind", ["missing", "directory", "invalid-utf8", "oversize", "deep"])
def test_unusable_filter_files_write_nothing(home, tmp_path, kind, capsys):
    source = tmp_path / "filters.json"
    writers = {
        "directory": lambda: source.mkdir(),
        "invalid-utf8": lambda: source.write_bytes(b"\xff\xff"),
        "oversize": lambda: source.write_text(" " * 100_000 + "[]", encoding="utf-8"),
        "deep": lambda: source.write_text('{"not":' * 80 + '{"field":"x","exists":true}' + "}" * 80),
    }
    if kind in writers:
        writers[kind]()
    assert _run("subscribe", "alerts", "--filters-file", str(source)) == 1
    assert not (home / "webhook_subscriptions.json").exists()
    assert "Error:" in capsys.readouterr().out


def test_legacy_object_filter_and_existing_operator_set(home):
    from gateway.platforms.webhook_filters import WebhookRouteProcessor

    watchlist = home / "watchlist.json"
    watchlist.write_text('["accepted"]', encoding="utf-8")
    spec = {"all": [
        {"field": "value", "exists": True},
        {"field": "absent", "missing": True},
        {"field": "value", "not_equals": "rejected"},
        {"field": "value", "contains": "accept"},
        {"field": "value", "in": ["accepted"]},
        {"field": "value", "in_file": str(watchlist)},
        {"field": "value", "regex": "^accept"},
    ]}
    _run("subscribe", "alerts", "--filters", json.dumps(spec))
    route = _load_subscriptions()["alerts"]
    assert route["filters"] == spec
    assert WebhookRouteProcessor().route_filters_match(route, {"value": "accepted"}, "test", {})


def test_filter_updates_still_use_compare_and_swap(home, monkeypatch):
    from hermes_cli import webhook

    _run("subscribe", "alerts", "--filters", '[{"field":"x","equals":1}]')
    original = webhook._replace_subscription

    def concurrent_update(name, route, expected):
        _mutate_subscriptions(lambda routes: routes["alerts"].update(description="concurrent"))
        return original(name, route, expected)

    monkeypatch.setattr(webhook, "_replace_subscription", concurrent_update)
    assert _run("subscribe", "alerts", "--filters", "[]") == 1
    assert _load_subscriptions()["alerts"]["filters"] == [{"field": "x", "equals": 1}]
    assert _load_subscriptions()["alerts"]["description"] == "concurrent"


def test_real_launcher_preserves_profile_isolation(home):
    worker = home / "profiles" / "worker"
    worker.mkdir(parents=True)
    (worker / "config.yaml").write_text(
        "platforms:\n  webhook:\n    enabled: true\n", encoding="utf-8"
    )
    env = {**os.environ, "HOME": str(home.parent), "HERMES_HOME": str(home)}
    for profile, value in [(None, "default-one"), ("worker", "worker-only"), (None, "default-two")]:
        args = [sys.executable, "-m", "hermes_cli.main"]
        if profile:
            args.extend(["-p", profile])
        args.extend([
            "webhook", "subscribe", "same-name", "--filters",
            json.dumps([{"field": "value", "equals": value}]),
        ])
        result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=60, check=False)
        assert result.returncode == 0, result.stderr
    default_route = json.loads((home / "webhook_subscriptions.json").read_text())["same-name"]
    worker_route = json.loads((worker / "webhook_subscriptions.json").read_text())["same-name"]
    assert default_route["filters"] == [{"field": "value", "equals": "default-two"}]
    assert worker_route["filters"] == [{"field": "value", "equals": "worker-only"}]
