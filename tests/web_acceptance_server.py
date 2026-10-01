"""Isolated real-HTTP browser fixture; model responses are deterministic, not live AI.

Only run this helper for acceptance tests. It neither uses user research storage
nor connects to a provider or live Word document.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from paperflow.application.services import ApplicationServices
from paperflow.gui.secrets import SecretStore
from paperflow.gui.server import create_app
from test_literature_service import pdf_bytes
from test_web_app_integration import EvidenceWritingModel


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--data-dir", required=True)
    args = parser.parse_args()
    root = Path(args.data_dir).resolve()
    services = ApplicationServices(data_dir=root)
    workspace = services.workspaces.create("隔离验收：受控模型，无外部请求", "浏览器端到端测试专用，不是真实科研结论。")
    pdf = pdf_bytes(["A real reproducible method.", "Observed baseline in the referenced study."])
    asset = services.assets.upload(workspace["id"], "acceptance.pdf", "application/pdf", pdf)
    model = EvidenceWritingModel(asset["asset_id"])
    secrets = SecretStore()
    secrets.configure("openai_compatible", "https://model.example.test/v1", "deterministic-browser-fixture", "fixture-only-not-a-provider-key", False)
    secrets.consent(True)

    def transport(url, headers, body):
        if not body.get("tools"):
            return {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]}
        return model(url, headers, body)

    app = create_app("isolated-browser-token", args.port, finder=services.finder, store=secrets,
                     literature=services.literature, writing=services.writing, loop=services.loop,
                     application=services, transport=transport, agent_transport=transport, standalone=True)
    print(f"http://127.0.0.1:{args.port}/?token=isolated-browser-token", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
