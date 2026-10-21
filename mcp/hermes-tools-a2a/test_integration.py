"""Exercise real Hermes/MCP dispatch against a local A2A fixture, not live agents.

Run with the Hermes venv Python and HERMES_SOURCE_DIR pointing at its source.
"""

from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from hermes_tools_a2a import A2A_TOOLS


WRAPPER = Path(__file__).with_name("hermes_tools_a2a.py").resolve()
HTTPS_PLUGIN = WRAPPER.parents[2] / "plugins" / "a2a-https-hotfix"
DIRECTORY_PLUGIN = WRAPPER.parents[2] / "plugins" / "a2a-peer-directory"
sys.path.insert(0, str(HTTPS_PLUGIN))
from https_transport import tls_server
from test_https import certificate


class Peer(BaseHTTPRequestHandler):
    """A deterministic HTTP peer to check headers, context and error propagation."""
    def log_message(self, *_args):
        pass

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        name = self.path.split("/")[1]
        url = getattr(self.server, "card_rpc_url", f"https://127.0.0.1:{self.server.server_port}/{name}")
        self.reply(200, {"name": name, "url": url,
                         "skills": [{"id": "echo", "name": "echo", "description": "Test fixture"}]})

    def do_POST(self):
        name = self.path.strip("/")
        if self.headers.get("Authorization") != f"Bearer fixture-{name}":
            self.reply(401, {"error": "fixture auth denied"})
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append((name, body))
        msg = body["params"]["message"]
        text = " ".join(p.get("text", "") for p in msg["parts"])
        if text == "RPC_ERROR":
            self.reply(200, {"jsonrpc": "2.0", "id": body["id"],
                             "error": {"code": -32000, "message": "fixture dispatch failed"}})
            return
        state = text if text in {"FAILED", "REJECTED", "CANCELED", "INPUT_REQUIRED"} else "COMPLETED"
        self.reply(200, {"jsonrpc": "2.0", "id": body["id"], "result": {
            "id": "fixture-task", "contextId": msg["contextId"],
            "status": {"state": "TASK_STATE_" + state},
            "artifacts": [{"parts": [{"text": f"{name} received: {text}"}]}],
        }})


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        source = os.environ.get("HERMES_SOURCE_DIR")
        if not source:
            self.fail("Set HERMES_SOURCE_DIR to the Hermes source checkout")
        self.source = Path(source).resolve()
        self.tmp = tempfile.TemporaryDirectory(prefix="hermes-a2a-mcp-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.default_home = self.root / ("AppData/Local/hermes" if os.name == "nt" else ".hermes")
        self.default_home.parent.mkdir(parents=True, exist_ok=True)
        self.cert, self.key = certificate(self.root)
        with patch.dict(os.environ, {"A2A_PUBLIC_URL": "https://127.0.0.1"}):
            self.peer = tls_server(ThreadingHTTPServer, self.cert, self.key)(("127.0.0.1", 0), Peer)
        self.peer.requests = []
        self.thread = threading.Thread(target=self.peer.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_peer)

    def stop_peer(self):
        self.peer.shutdown()
        self.peer.server_close()
        self.thread.join(timeout=5)

    def home(self, name, enabled=True):
        home = self.root / "profiles" / name
        home.mkdir(parents=True)
        shutil.copytree(HTTPS_PLUGIN, home / "plugins" / HTTPS_PLUGIN.name)
        cfg = {"plugins": {"enabled": ["a2a-https-hotfix"], "disabled": ["a2a-platform"],
                           "entries": {"a2a-https-hotfix": {"settings": {
                               "ca_file": self.cert, "cert_file": self.cert, "key_file": self.key,
                           }}}}}
        if enabled:
            cfg["a2a_agents"] = {f"peer-{name}": {
                "url": f"https://127.0.0.1:{self.peer.server_port}/{name}",
                "auth": {"type": "bearer", "token": f"fixture-{name}"},
                "capabilities": ["echo"], "timeout": 5,
            }}
        # JSON is valid YAML and avoids touching the user's configuration.
        (home / "config.yaml").write_text(json.dumps(cfg), encoding="utf-8")
        return home

    @asynccontextmanager
    async def client(self, home, wrapped=True, forward_home=True):
        env = {k: v for k, v in os.environ.items()
               if k in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "LANG")}
        env.update(HOME=str(self.root), USERPROFILE=str(self.root), HERMES_HOME=str(home),
                   LOCALAPPDATA=str(self.root / "AppData" / "Local"),
                   PYTHONPATH=str(self.source), HERMES_QUIET="1", HERMES_REDACT_SECRETS="true",
                   PYTHONDONTWRITEBYTECODE="1")
        if not forward_home:
            env.pop("HERMES_HOME")
        args = [str(WRAPPER)] if wrapped else ["-m", "agent.transports.hermes_tools_mcp_server"]
        params = StdioServerParameters(command=sys.executable, args=args, env=env, cwd=str(home))
        with open(os.devnull, "w") as errors:
            async with stdio_client(params, errlog=errors) as (reader, writer):
                async with ClientSession(reader, writer, read_timeout_seconds=30) as client:
                    await client.initialize()
                    yield client

    async def call(self, client, name, args):
        result = await client.call_tool(name, args)
        self.assertFalse(result.is_error, result)
        return "\n".join(c.text for c in result.content if c.type == "text")

    async def test_opt_in_adds_only_a2a_and_preserves_required_arguments(self):
        home = self.home("alpha")
        async with self.client(home, wrapped=False) as client:
            baseline = {t.name for t in (await client.list_tools()).tools}
        async with self.client(home) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            self.assertEqual(set(tools), baseline | set(A2A_TOOLS))
            self.assertEqual(set(tools["a2a_call"].input_schema["required"]), {"agent", "message"})
            invalid = await client.call_tool("a2a_call", {"agent": "peer-alpha"})
            self.assertTrue(invalid.is_error)
        self.assertEqual(self.peer.requests, [])
        disabled = self.home("disabled", enabled=False)
        async with self.client(disabled) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            self.assertFalse(names & set(A2A_TOOLS))

    async def test_unscoped_codex_exports_a2a_using_default_home(self):
        home = self.home("desktop")
        home.rename(self.default_home)
        # A profile-like working directory must not select its peer configuration.
        home = self.home("other")
        async with self.client(home, wrapped=False, forward_home=False) as client:
            baseline = {t.name for t in (await client.list_tools()).tools}
        async with self.client(home, forward_home=False) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            self.assertEqual(names, baseline | set(A2A_TOOLS))
            self.assertIn("skills_list", names)
            listing = await self.call(client, "a2a_list", {})
            self.assertIn("peer-desktop", listing)
            self.assertNotIn("peer-other", listing)
            result = await self.call(client, "a2a_call", {
                "agent": "peer-desktop", "message": "unscoped roundtrip", "context_id": "desktop-test",
            })
            self.assertIn("desktop received: unscoped roundtrip", result)
        self.assertTrue((self.default_home / "a2a_conversations/desktop-test.jsonl").exists())
        self.assertFalse((home / "a2a_conversations/desktop-test.jsonl").exists())

    async def test_unscoped_codex_ignores_parent_local_appdata(self):
        inherited = self.root / "parent-appdata"
        inherited.mkdir()
        # The MCP SDK inherits LOCALAPPDATA on Windows even when env omits it.
        with patch.dict(os.environ, {"LOCALAPPDATA": str(inherited)}):
            try:
                await self.test_unscoped_codex_exports_a2a_using_default_home()
            finally:
                self.assertEqual(list(inherited.iterdir()), [],
                                 "MCP child touched the parent's Windows app-data directory")

    async def test_peer_directory_metadata_alias_and_capability_routing(self):
        for name in ("alpha", "beta"):
            home = self.home(name)
            shutil.copytree(DIRECTORY_PLUGIN, home / "plugins" / DIRECTORY_PLUGIN.name)
            cfg = json.loads((home / "config.yaml").read_text())
            cfg["plugins"]["enabled"].append("a2a-peer-directory")
            cfg["plugins"]["entries"]["a2a-peer-directory"] = {"allow_tool_override": True}
            peer = cfg["a2a_agents"].pop(f"peer-{name}")
            peer.update(description=f"{name} 产品资料与故障排查", capabilities=["product-support", "troubleshooting"])
            cfg["a2a_agents"]["product-support"] = peer
            (home / "config.yaml").write_text(json.dumps(cfg), encoding="utf-8")
            async with self.client(home) as client:
                schema = next(t for t in (await client.list_tools()).tools if t.name == "a2a_list")
                self.assertIn("responsibility descriptions", schema.description)
                listing = await self.call(client, "a2a_list", {})
                self.assertIn(f"product-support: {name} 产品资料与故障排查", listing)
                self.assertIn("caps: product-support, troubleshooting", listing)
                self.assertIn("Metrics:", listing)
                self.assertNotIn(f"fixture-{name}", listing)
                self.assertNotIn("beta 产品资料" if name == "alpha" else "alpha 产品资料", listing)
                result = await self.call(client, "a2a_call", {
                    "agent": "product-support", "message": "directory check", "context_id": "directory-test",
                })
                self.assertIn(f"{name} received: directory check", result)
                old = await client.call_tool("a2a_call", {"agent": f"peer-{name}", "message": "must not route"})
                self.assertTrue(old.is_error)
                result = await self.call(client, "a2a_orchestrate", {
                    "capability": "troubleshooting", "message": "capability check", "mode": "all",
                })
                self.assertIn(f"{name} received: capability check", result)
                listing = await self.call(client, "a2a_list", {})
                self.assertIn("directory-test", listing)
                self.assertIn("Peer responsibilities (configured):", listing)

    async def test_real_handlers_keep_profiles_auth_and_conversations_separate(self):
        homes = [self.home(name) for name in ("alpha", "beta")]
        for home in homes:
            name = home.name
            other = "beta" if name == "alpha" else "alpha"
            async with self.client(home) as client:
                peers = await self.call(client, "a2a_list", {})
                self.assertIn(f"peer-{name}", peers)
                self.assertNotIn(f"peer-{other}", peers)
                self.assertNotIn(f"fixture-{name}", peers)
                url = f"https://127.0.0.1:{self.peer.server_port}/{name}"
                card = await self.call(client, "a2a_discover", {"url": url})
                self.assertIn(f"Agent: {name}", card)
                message = f"hello-{name}: quotes ' \" ` $(literal)"
                result = await self.call(client, "a2a_call", {
                    "agent": f"peer-{name}", "message": message, "context_id": "shared-test-context",
                })
                self.assertIn(f"{name} received: {message}", result)
                history = await self.call(client, "a2a_history", {"context_id": "shared-test-context"})
                self.assertIn(f"hello-{name}", history)
                self.assertNotIn(f"hello-{other}", history)
                result = await self.call(client, "a2a_orchestrate", {
                    "capability": "echo", "message": f"batch-{name}", "mode": "all",
                })
                self.assertIn(f"{name} received: batch-{name}", result)
                # A direct URL has no configured bearer: preserve upstream's refusal.
                denied = await client.call_tool("a2a_call", {"agent": url, "message": "denied"})
                self.assertTrue(denied.is_error)
                self.assertIn("HTTP 401", denied.content[0].text)
            self.assertTrue((home / "a2a_conversations" / "shared-test-context.jsonl").exists())
        self.assertEqual(len(self.peer.requests), 4)
        for name, body in self.peer.requests:
            self.assertEqual(body["method"], "SendMessage")
            if body["params"]["message"]["parts"][0]["text"].startswith("hello-"):
                self.assertEqual(body["params"]["message"]["contextId"], "shared-test-context")

    async def test_rejected_agent_card_sets_mcp_error_for_registry_json(self):
        home = self.home("card-errors")
        async with self.client(home) as client:
            for target in ("https://other.example/rpc", "http://127.0.0.1/rpc"):
                with self.subTest(target=target):
                    self.peer.card_rpc_url = target
                    result = await client.call_tool("a2a_discover", {
                        "url": f"https://127.0.0.1:{self.peer.server_port}/card-errors",
                    })
                    payload = json.loads(result.content[0].text)
                    self.assertIn("[TOOL_ERROR]", payload["error"])
                    self.assertTrue(result.is_error, result)
                    self.assertEqual(result.structured_content, {"result": result.content[0].text})
        self.assertEqual(self.peer.requests, [], "Rejected cards must not dispatch a task")

    async def test_a2a_failures_set_mcp_is_error(self):
        home = self.home("errors")
        async with self.client(home) as client:
            for message in ("FAILED", "REJECTED", "CANCELED", "RPC_ERROR"):
                result = await client.call_tool("a2a_call", {"agent": "peer-errors", "message": message})
                self.assertTrue(result.is_error, (message, result))
                self.assertTrue(result.content[0].text.startswith("Error:"), result)
                self.assertNotIn("validation error", result.content[0].text)
            for message in ("INPUT_REQUIRED", "Error: harmless quoted example",
                            '{"error": "harmless quoted example"}'):
                result = await client.call_tool("a2a_call", {"agent": "peer-errors", "message": message})
                self.assertFalse(result.is_error, result)
                self.assertIn(message, result.content[0].text)
            denied = await client.call_tool("a2a_call", {
                "agent": f"https://127.0.0.1:{self.peer.server_port}/errors", "message": "no credentials"})
            self.assertTrue(denied.is_error, denied)
            for mode in ("first", "best"):
                result = await client.call_tool("a2a_orchestrate", {
                    "capability": "echo", "message": "FAILED", "mode": mode})
                self.assertTrue(result.is_error, result)

    @unittest.skipUnless(os.environ.get("CODEX_BINARY"), "Set CODEX_BINARY for app-server verification")
    async def test_codex_app_server_discovers_and_calls_a2a(self):
        await self.check_codex_app_server(forward_home=True)

    @unittest.skipUnless(os.environ.get("CODEX_BINARY"), "Set CODEX_BINARY for app-server verification")
    async def test_unscoped_codex_app_server_discovers_and_calls_a2a(self):
        await self.check_codex_app_server(forward_home=False)

    async def check_codex_app_server(self, forward_home):
        home = self.home("codex")
        if not forward_home:
            home.rename(self.default_home)
        codex_home = self.root / "codex-home"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text(
            'model_provider = "fixture"\nmodel = "fixture"\n'
            '[model_providers.fixture]\nname = "fixture"\n'
            'base_url = "http://127.0.0.1:1"\nwire_api = "responses"\n'
            'requires_openai_auth = false\n'
            '[mcp_servers.hermes-tools]\n'
            f'command = {json.dumps(sys.executable)}\nargs = [{json.dumps(str(WRAPPER))}]\n'
            'env_vars = ["HERMES_HOME"]\n'
            '[mcp_servers.hermes-tools.env]\n'
            f'PYTHONPATH = {json.dumps(str(self.source))}\n'
            'HERMES_QUIET = "1"\nHERMES_REDACT_SECRETS = "true"\n', encoding="utf-8",
        )
        env = {k: v for k, v in os.environ.items()
               if k in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "LANG")}
        env.update(HOME=str(self.root), USERPROFILE=str(self.root), CODEX_HOME=str(codex_home),
                   LOCALAPPDATA=str(self.root / "AppData" / "Local"),
                   PYTHONDONTWRITEBYTECODE="1")
        if forward_home:
            env["HERMES_HOME"] = str(home)
        proc = await asyncio.create_subprocess_exec(
            os.environ["CODEX_BINARY"], "app-server", cwd=self.root, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        sequence = 0

        async def request(method, params):
            nonlocal sequence
            sequence += 1
            proc.stdin.write((json.dumps({"id": sequence, "method": method, "params": params}) + "\n").encode())
            await proc.stdin.drain()
            async with asyncio.timeout(30):
                while True:
                    line = await proc.stdout.readline()
                    self.assertTrue(line, "app-server exited before its response")
                    response = json.loads(line)
                    if response.get("id") == sequence and "method" not in response:
                        self.assertNotIn("error", response)
                        return response["result"]
                    self.assertFalse("id" in response and "method" in response,
                                     "Unexpected app-server request during isolated test")

        try:
            await request("initialize", {"clientInfo": {"name": "hermes_a2a_test", "version": "1"},
                                         "capabilities": {"experimentalApi": True}})
            proc.stdin.write(b'{"method":"initialized","params":{}}\n')
            await proc.stdin.drain()
            started = await request("thread/start", {"cwd": str(self.root), "ephemeral": True,
                                                      "approvalPolicy": "never"})
            thread = started["thread"]["id"]
            inventory = await request("mcpServerStatus/list", {
                "threadId": thread, "serverName": "hermes-tools", "detail": "toolsAndAuthOnly",
            })
            server = next(s for s in inventory["data"] if s["name"] == "hermes-tools")
            self.assertTrue(set(A2A_TOOLS) <= set(server["tools"]),
                            {"runtimeStatus": server.get("runtimeStatus"), "tools": list(server["tools"])})
            result = await request("mcpServer/tool/call", {
                "threadId": thread, "server": "hermes-tools", "tool": "a2a_call",
                "arguments": {"agent": "peer-codex", "message": "app-server roundtrip", "context_id": "codex-test"},
            })
            self.assertFalse(result.get("isError"), result)
            text = "\n".join(c.get("text", "") for c in result["content"])
            self.assertIn("codex received: app-server roundtrip", text)
        finally:
            if proc.returncode is None:
                if os.name == "nt":
                    # Codex's Git children can still hold the temporary home open.
                    cleanup = await asyncio.create_subprocess_exec(
                        "taskkill", "/PID", str(proc.pid), "/T", "/F",
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    )
                    await cleanup.wait()
                else:
                    proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()


if __name__ == "__main__":
    unittest.main()
