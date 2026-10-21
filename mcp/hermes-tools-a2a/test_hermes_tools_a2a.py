"""Self-contained coverage of the startup patch; no Hermes install needed."""

import io
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

from hermes_tools_a2a import A2A_TOOLS, main, with_a2a_tools


def definition(name, description="upstream schema"):
    return {"type": "function", "function": {"name": name, "description": description}}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.upstream = types.ModuleType("agent.transports.hermes_tools_mcp_server")
        self.upstream.EXPOSED_TOOLS = ("web_search",)
        self.base = [definition("web_search"), definition("tool_search")]
        self.a2a = [definition(name) for name in A2A_TOOLS]
        self.model_tools = types.ModuleType("model_tools")
        self.original = Mock(side_effect=lambda **kw: self.a2a if kw.get("enabled_toolsets") else self.base)
        self.model_tools.handle_function_call = Mock(return_value="ok")
        self.model_tools.get_tool_definitions = self.original
        transports = types.ModuleType("agent.transports")
        transports.hermes_tools_mcp_server = self.upstream
        modules = {"agent": types.ModuleType("agent"), "agent.transports": transports,
                   "agent.transports.hermes_tools_mcp_server": self.upstream,
                   "model_tools": self.model_tools}
        patcher = patch.dict(sys.modules, modules)
        patcher.start()
        self.addCleanup(patcher.stop)

    def build(self):
        definitions = self.model_tools.get_tool_definitions(quiet_mode=True)
        return [item for item in definitions if item["function"]["name"] in self.upstream.EXPOSED_TOOLS]

    def test_adds_five_raw_schemas_without_exposing_other_tools(self):
        self.a2a.append(definition("terminal"))
        result = with_a2a_tools(self.build)()
        self.assertEqual([d["function"]["name"] for d in result], ["web_search", *A2A_TOOLS])
        self.assertIs(result[1], self.a2a[0])
        self.assertEqual(len(self.base), 2)  # Do not modify upstream's cached list.
        self.original.assert_any_call(enabled_toolsets=["a2a"], quiet_mode=True, skip_tool_search_assembly=True)
        self.assertIs(self.model_tools.get_tool_definitions, self.original)
        self.assertEqual(self.upstream.EXPOSED_TOOLS, ("web_search",))

    def test_unconfigured_or_disabled_a2a_stays_hidden(self):
        self.a2a = []
        self.assertEqual(with_a2a_tools(self.build)(), [self.base[0]])

    def test_existing_upstream_a2a_wins_without_duplicates(self):
        native = definition("a2a_call", "native support")
        self.base.append(native)
        self.upstream.EXPOSED_TOOLS += ("a2a_call",)
        result = with_a2a_tools(self.build)()
        calls = [d for d in result if d["function"]["name"] == "a2a_call"]
        self.assertEqual(calls, [native])

    def test_restores_globals_after_build_failure(self):
        def fail():
            raise RuntimeError("build failed")
        with self.assertRaisesRegex(RuntimeError, "build failed"):
            with_a2a_tools(fail)()
        self.assertIs(self.model_tools.get_tool_definitions, self.original)
        self.assertEqual(self.upstream.EXPOSED_TOOLS, ("web_search",))

    def test_main_preserves_upstream_runner_and_restores_builder(self):
        self.upstream._build_server = self.build
        original = self.upstream._build_server
        self.upstream.main = Mock(side_effect=lambda: len(self.upstream._build_server()))
        with patch.dict(os.environ, {"HERMES_HOME": str(Path.cwd())}):
            self.assertEqual(main(), 6)
        self.assertIs(self.upstream._build_server, original)
        self.upstream.main.assert_called_once_with()

    def test_relative_home_is_rejected(self):
        with patch.dict(os.environ, {"HERMES_HOME": "relative-profile"}), \
                patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(main(), 2)
            self.assertIn("HERMES_HOME", stderr.getvalue())

    def test_unscoped_codex_exports_a2a_without_binding_a_profile(self):
        self.base.append(definition("a2a_call"))
        self.upstream.EXPOSED_TOOLS += ("a2a_call",)
        original_exports = self.upstream.EXPOSED_TOOLS
        self.upstream._build_server = self.build
        original_builder = self.upstream._build_server
        self.upstream.main = Mock(side_effect=lambda: self.upstream._build_server())
        for value in (None, ""):
            with self.subTest(home=value), patch.dict(os.environ):
                if value is None:
                    os.environ.pop("HERMES_HOME", None)
                else:
                    os.environ["HERMES_HOME"] = value
                self.assertEqual({d["function"]["name"] for d in main()},
                                 {"web_search", *A2A_TOOLS})
                self.assertEqual(os.environ.get("HERMES_HOME"), value)
        self.assertEqual(self.upstream.EXPOSED_TOOLS, original_exports)
        self.assertIs(self.upstream._build_server, original_builder)
        self.assertIs(self.model_tools.get_tool_definitions, self.original)


if __name__ == "__main__":
    unittest.main()
