"""Run the upstream hermes-tools MCP with its available A2A tools added."""

from __future__ import annotations

from functools import wraps
import json
import os
from pathlib import Path
import sys


A2A_TOOLS = (
    "a2a_discover",
    "a2a_list",
    "a2a_call",
    "a2a_history",
    "a2a_orchestrate",
)


def with_error_status(dispatch):
    @wraps(dispatch)
    def call(name, *args, **kwargs):
        if name not in A2A_TOOLS:
            return dispatch(name, *args, **kwargs)
        from mcp.types import CallToolResult, TextContent
        try:
            result = dispatch(name, *args, **kwargs)
        except Exception as exc:
            result = f"Error: {exc}"
        # These are native tool envelopes, never keywords inside a peer reply.
        is_error = isinstance(result, str) and result.startswith(("Error:", "All peers failed:"))
        if isinstance(result, str) and not is_error:
            try:
                envelope = json.loads(result)
            except json.JSONDecodeError:
                pass
            else:
                # Registry dispatch serializes uncaught handler exceptions.
                is_error = isinstance(envelope, dict) and isinstance(envelope.get("error"), str)
        if is_error:
            return CallToolResult(content=[TextContent(type="text", text=result)],
                                  structuredContent={"result": result}, isError=True)
        return result
    return call


def with_a2a_tools(build_server):
    """Supplement only A2A schemas while upstream builds its normal server."""
    @wraps(build_server)
    def build():
        import model_tools
        from agent.transports import hermes_tools_mcp_server as upstream

        original_definitions = model_tools.get_tool_definitions
        original_exports = upstream.EXPOSED_TOOLS
        original_dispatch = model_tools.handle_function_call

        def definitions(*args, **kwargs):
            result = list(original_definitions(*args, **kwargs))
            seen = {item["function"]["name"] for item in result}
            # Tool-search compaction can hide A2A from the MCP's fixed exports.
            for item in original_definitions(
                enabled_toolsets=["a2a"], quiet_mode=True,
                skip_tool_search_assembly=True,
            ):
                name = item["function"]["name"]
                if name in A2A_TOOLS and name not in seen:
                    result.append(item)
                    seen.add(name)
            return result

        try:
            upstream.EXPOSED_TOOLS = tuple(dict.fromkeys((*original_exports, *A2A_TOOLS)))
            model_tools.get_tool_definitions = definitions
            model_tools.handle_function_call = with_error_status(original_dispatch)
            return build_server()
        finally:
            model_tools.get_tool_definitions = original_definitions
            model_tools.handle_function_call = original_dispatch
            upstream.EXPOSED_TOOLS = original_exports

    return build


def main():
    home = os.environ.get("HERMES_HOME", "").strip()
    if home and not Path(home).is_absolute():
        print("hermes-tools A2A requires an absolute HERMES_HOME; forward it with "
              'mcp_servers.hermes-tools.env_vars = ["HERMES_HOME"].', file=sys.stderr)
        return 2

    from agent.transports import hermes_tools_mcp_server as upstream

    original_builder = upstream._build_server
    try:
        upstream._build_server = with_a2a_tools(original_builder)
        return upstream.main()
    finally:
        upstream._build_server = original_builder


if __name__ == "__main__":
    raise SystemExit(main())
