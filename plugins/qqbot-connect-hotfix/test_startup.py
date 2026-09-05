"""Plugin discovery must not wait on GatewayRunner's in-progress import."""
import builtins
import importlib.util
from pathlib import Path
import sys
from unittest.mock import patch

path = Path(__file__).with_name('__init__.py')
spec = importlib.util.spec_from_file_location('qq_startup_test', path, submodule_search_locations=[str(path.parent)])
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)


def main():
    original_import = builtins.__import__

    def reject_gateway_import(name, *args, **kwargs):
        assert name != 'gateway.run', 'plugin-discovery can deadlock importing gateway.run while main waits on plugin discovery'
        return original_import(name, *args, **kwargs)

    with patch('builtins.__import__', reject_gateway_import):
        plugin.register(None)

    from gateway.config import PlatformConfig
    from gateway.platforms.qqbot.adapter import QQAdapter
    init = QQAdapter.__init__
    plugin.register(None)
    assert QQAdapter.__init__ is init
    # The first adapter is created after Gateway import, before any message.
    # Patches must be ready for the first message, not deferred until /new.
    QQAdapter(PlatformConfig(extra={'app_id': 'test-app', 'client_secret': 'test-secret'}))
    from gateway.run import GatewayRunner
    assert getattr(GatewayRunner._deliver_media_from_response, '_qqbot_post_stream_failure_wrapped', False)
    assert callable(QQAdapter.supports_draft_streaming)
    print('discovery import safety and first-adapter Gateway patch activation: PASS')


if __name__ == '__main__':
    main()
