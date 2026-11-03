"""A CLI subscription filters signed HTTP deliveries before durable admission (#144398)."""

import hashlib
import hmac
import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter
from hermes_cli._parser import build_top_level_parser
from hermes_cli.subcommands.webhook import build_webhook_parser
from hermes_cli.webhook import _load_subscriptions, webhook_command
from tests.gateway.fixtures.webhook_route_authority import mount_authority


@pytest.mark.asyncio
async def test_saved_filters_gate_signed_requests_after_reload(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "platforms:\n  webhook:\n    enabled: true\n", encoding="utf-8"
    )
    parser, subparsers, _ = build_top_level_parser()
    build_webhook_parser(subparsers, cmd_webhook=webhook_command)

    def subscribe(filters):
        args = parser.parse_args([
            "webhook", "subscribe", "merged", "--events", "pull_request",
            "--prompt", "Merged #{pull_request.number}", "--filters", json.dumps(filters),
        ])
        assert args.func(args) is None

    subscribe([
        {"field": "action", "equals": "closed"},
        {"field": "pull_request.merged", "equals": True},
    ])
    secret = _load_subscriptions()["merged"]["secret"]
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"host": "127.0.0.1"}))
    captured = []

    async def capture(event):
        captured.append(event)

    adapter.handle_message = capture
    app = web.Application(client_max_size=adapter._max_body_bytes)
    mount_authority(app, adapter)
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    async with TestClient(TestServer(app)) as client:
        async def post(action, merged, delivery, *, key=secret):
            body = json.dumps({
                "action": action, "pull_request": {"merged": merged, "number": 42},
            }).encode()
            signature = hmac.new(key.encode(), body, hashlib.sha256).hexdigest()
            return await client.post("/webhooks/merged", data=body, headers={
                "X-Hub-Signature-256": "sha256=" + signature,
                "X-GitHub-Event": "pull_request", "X-GitHub-Delivery": delivery,
                "Content-Type": "application/json",
            })

        rejected = await post("opened", False, "same-delivery")
        assert rejected.status == 200
        assert await rejected.json() == {"status": "ignored", "reason": "filter", "route": "merged"}
        assert captured == []
        assert not adapter._background_tasks
        # A filtered delivery has not spent idempotency or admitted an agent turn.
        accepted = await post("closed", True, "same-delivery")
        assert accepted.status == 202
        assert [event.text for event in captured] == ["Merged #42"]
        unauthorized = await post("closed", True, "bad-signature", key="synthetic-wrong-key")
        assert unauthorized.status == 401
        assert len(captured) == 1
        # The same running adapter sees an explicit clear through its normal hot reload.
        subscribe([])
        unfiltered = await post("opened", False, "after-clear")
        assert unfiltered.status == 202
        assert len(captured) == 2
