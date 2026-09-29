"""Run against the installed Hermes Dashboard in a temporary profile tree."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import tempfile


with tempfile.TemporaryDirectory(prefix="dashboard-profile-logs-test-") as temp:
    root = Path(temp)
    os.environ["HERMES_HOME"] = temp
    name = "dashboard-profile-logs"
    shutil.copytree(Path(__file__).parent, root / "plugins" / name,
                    ignore=shutil.ignore_patterns("__pycache__"))
    for profile in ("default", "product", "procurement"):
        home = root if profile == "default" else root / "profiles" / profile
        (home / "logs").mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text(f"plugins:\n  enabled: [{name}]\n")
        for file in ("agent", "errors", "gateway"):
            (home / "logs" / (file + ".log")).write_text(
                f"2026-09-29 12:00:00 ERROR gateway.run: {profile}-{file}-gateway-error\n"
                f"2026-09-29 12:00:01 INFO gateway.run: {profile}-{file}-gateway-info\n"
                f"2026-09-29 12:00:02 ERROR run_agent: {profile}-{file}-agent-error\n"
                f"2026-09-29 12:00:03 INFO run_agent: {profile}-{file}-agent-info\n"
            )

    # Import after choosing the fixture home: no live profile or credentials.
    from hermes_cli import web_server
    from hermes_constants import get_hermes_home
    import httpx

    async def main():
        endpoint = "/api/plugins/dashboard-profile-logs/logs"
        transport = httpx.ASGITransport(app=web_server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            assert (await client.get(endpoint)).status_code == 401
            client.headers["X-Hermes-Session-Token"] = web_server._SESSION_TOKEN
            for profile in ("default", "product", "procurement"):
                for file in ("agent", "errors", "gateway"):
                    response = await client.get(endpoint, params={"profile": profile, "file": file})
                    assert response.status_code == 200, response.text
                    data = response.json()
                    assert len(data["lines"]) == 4
                    assert all(profile + "-" + file in line for line in data["lines"]), data
            response = await client.get(endpoint, params={"profile": "product", "level": "ERROR", "lines": 10})
            assert response.json()["lines"] == [
                "2026-09-29 12:00:00 ERROR gateway.run: product-agent-gateway-error\n",
                "2026-09-29 12:00:02 ERROR run_agent: product-agent-agent-error\n",
            ]
            for component, logger in (("gateway", "gateway.run"), ("agent", "run_agent")):
                response = await client.get(endpoint, params={"profile": "product", "component": component, "lines": 10})
                selected = response.json()["lines"]
                assert len(selected) == 2 and all(logger + ":" in line for line in selected)
            response = await client.get(endpoint, params={"profile": "product", "component": "gateway", "level": "ERROR"})
            assert response.json()["lines"] == ["2026-09-29 12:00:00 ERROR gateway.run: product-agent-gateway-error\n"]
            response = await client.get(endpoint, params={"profile": "procurement", "search": "info"})
            assert len(response.json()["lines"]) == 2 and all("procurement" in line for line in response.json()["lines"])
            response = await client.get(endpoint, params={"profile": "product", "lines": 1})
            assert response.json()["lines"] == ["2026-09-29 12:00:03 INFO run_agent: product-agent-agent-info\n"]
            for profile, status in (("../product", 400), ("absent", 404)):
                assert (await client.get(endpoint, params={"profile": profile})).status_code == status
            assert (await client.get(endpoint, params={"file": "../config.yaml"})).status_code == 400
            (root / "profiles/product/logs/errors.log").unlink()
            assert (await client.get(endpoint, params={"profile": "product", "file": "errors"})).json()["lines"] == []
            responses = await asyncio.gather(*(
                client.get(endpoint, params={"profile": profile})
                for profile in ["default", "product", "procurement"] * 4
            ))
            for profile, response in zip(["default", "product", "procurement"] * 4, responses):
                assert all(profile + "-agent" in line for line in response.json()["lines"])
            assert get_hermes_home() == root
            assert (await client.get(endpoint)).json()["profile"] == "current"
            # Removing enablement immediately revokes this plugin's API access.
            (root / "config.yaml").write_text("plugins:\n  enabled: []\n")
            assert (await client.get(endpoint)).status_code == 404
        print(json.dumps({"profile_file_cases": 9, "concurrent_cases": 12, "auth_filters_invalid_missing": "passed"}))

    asyncio.run(main())
