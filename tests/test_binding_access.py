import sys
import types
import unittest
import json
from unittest.mock import patch


def _install_astrbot_stubs() -> None:
    if "astrbot" in sys.modules:
        return

    astrbot_module = types.ModuleType("astrbot")
    api_module = types.ModuleType("astrbot.api")
    api_all_module = types.ModuleType("astrbot.api.all")
    event_module = types.ModuleType("astrbot.api.event")
    star_module = types.ModuleType("astrbot.api.star")
    core_tool_module = types.ModuleType("astrbot.core.agent.tool")
    core_run_context_module = types.ModuleType("astrbot.core.agent.run_context")
    core_agent_context_module = types.ModuleType("astrbot.core.astr_agent_context")

    class _Logger:
        def info(self, *args, **kwargs):
            return None

        def warning(self, *args, **kwargs):
            return None

        def error(self, *args, **kwargs):
            return None

    class _DummyCommandGroup:
        def __call__(self, func):
            func.command = self.command
            return func

        def command(self, *_args, **_kwargs):
            def decorator(func):
                return func

            return decorator

    class _PermissionType:
        ADMIN = "admin"

    class _Filter:
        PermissionType = _PermissionType

        @staticmethod
        def command_group(_name):
            return _DummyCommandGroup()

        @staticmethod
        def permission_type(_permission):
            def decorator(func):
                return func

            return decorator

    class _Star:
        def __init__(self, context=None):
            self.context = context

    class _Context:
        def add_llm_tools(self, *_args, **_kwargs):
            return None

    class _AstrBotConfig(dict):
        pass

    class _FunctionTool:
        @classmethod
        def __class_getitem__(cls, _item):
            return cls

    class _ContextWrapper:
        @classmethod
        def __class_getitem__(cls, _item):
            return cls

    class _AstrAgentContext:
        pass

    def _register(*_args, **_kwargs):
        def decorator(cls):
            return cls

        return decorator

    api_module.logger = _Logger()
    api_module.AstrBotConfig = _AstrBotConfig
    event_module.filter = _Filter
    event_module.AstrMessageEvent = object
    star_module.Context = _Context
    star_module.Star = _Star
    star_module.register = _register
    core_tool_module.FunctionTool = _FunctionTool
    core_run_context_module.ContextWrapper = _ContextWrapper
    core_agent_context_module.AstrAgentContext = _AstrAgentContext

    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = api_module
    sys.modules["astrbot.api.all"] = api_all_module
    sys.modules["astrbot.api.event"] = event_module
    sys.modules["astrbot.api.star"] = star_module
    sys.modules["astrbot.core.agent.tool"] = core_tool_module
    sys.modules["astrbot.core.agent.run_context"] = core_run_context_module
    sys.modules["astrbot.core.astr_agent_context"] = core_agent_context_module


def _install_pydantic_stubs() -> None:
    if "pydantic" in sys.modules:
        return

    pydantic_module = types.ModuleType("pydantic")
    pydantic_dataclasses_module = types.ModuleType("pydantic.dataclasses")

    def _field(*, default=None, default_factory=None, **_kwargs):
        if default_factory is not None:
            return default_factory()
        return default

    def _dataclass(cls=None, **_kwargs):
        def wrap(inner_cls):
            annotations = getattr(inner_cls, "__annotations__", {})

            def __init__(self, **kwargs):
                for name in annotations:
                    if name in kwargs:
                        value = kwargs[name]
                    else:
                        value = getattr(inner_cls, name, None)
                    setattr(self, name, value)

            inner_cls.__init__ = __init__
            return inner_cls

        if cls is None:
            return wrap
        return wrap(cls)

    pydantic_module.Field = _field
    pydantic_dataclasses_module.dataclass = _dataclass

    sys.modules["pydantic"] = pydantic_module
    sys.modules["pydantic.dataclasses"] = pydantic_dataclasses_module


_install_astrbot_stubs()
_install_pydantic_stubs()

from main import EmbyPlugin


class _DummyEvent:
    def __init__(self, sender_id: str):
        self._sender_id = sender_id

    def get_sender_id(self) -> str:
        return self._sender_id

    def plain_result(self, text: str) -> str:
        return text


class _DummyContext:
    def __init__(self):
        self._tools = []

    def add_llm_tools(self, *_args, **_kwargs):
        self._tools.extend(_args)
        return None


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeAsyncClient:
    last_request = None

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, headers=None, params=None):
        type(self).last_request = {
            "url": url,
            "headers": headers,
            "params": params,
        }
        return _FakeResponse({"Items": []})


class EmbyBindingAccessTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.context = _DummyContext()
        config = {
            "emby_host": "http://emby.test",
            "api_key": "token",
            "search_limit": 10,
            "latest_limit": 10,
        }
        self.plugin = EmbyPlugin(self.context, config)

    async def test_unbound_uid_is_rejected_for_items_requests(self):
        with patch.object(self.plugin, "_get_bindings", return_value={}):
            result = await self.plugin.api_request("Items", {"Limit": 5}, _DummyEvent("10001"))

        self.assertEqual(
            result,
            {"error": "当前 UID 未绑定 Emby 用户，请联系管理员执行 /emby add"},
        )

    async def test_bound_uid_uses_user_scoped_emby_endpoint(self):
        bindings = {"10001": {"id": "emby-user-1", "name": "alice"}}

        with patch.object(self.plugin, "_get_bindings", return_value=bindings), patch(
            "main.httpx.AsyncClient", _FakeAsyncClient
        ):
            result = await self.plugin.api_request("Items", {"Limit": 5}, _DummyEvent("10001"))

        self.assertEqual(result, {"Items": []})
        self.assertEqual(
            _FakeAsyncClient.last_request["url"],
            "http://emby.test/emby/Users/emby-user-1/Items",
        )

    async def test_search_command_surfaces_binding_error(self):
        async def _api_request(*_args, **_kwargs):
            return {"error": "当前 UID 未绑定 Emby 用户，请联系管理员执行 /emby add"}

        with patch.object(self.plugin, "api_request", side_effect=_api_request):
            results = [item async for item in self.plugin.emby_search(_DummyEvent("10001"), "test")]

        self.assertEqual(results, ["当前 UID 未绑定 Emby 用户，请联系管理员执行 /emby add"])

    async def test_latest_command_surfaces_binding_error(self):
        async def _api_request(*_args, **_kwargs):
            return {"error": "当前 UID 未绑定 Emby 用户，请联系管理员执行 /emby add"}

        with patch.object(self.plugin, "api_request", side_effect=_api_request):
            results = [item async for item in self.plugin.emby_latest(_DummyEvent("10001"))]

        self.assertEqual(results, ["当前 UID 未绑定 Emby 用户，请联系管理员执行 /emby add"])

    async def test_detail_command_surfaces_binding_error(self):
        async def _api_request(*_args, **_kwargs):
            return {"error": "当前 UID 未绑定 Emby 用户，请联系管理员执行 /emby add"}

        with patch.object(self.plugin, "api_request", side_effect=_api_request):
            results = [item async for item in self.plugin.emby_detail(_DummyEvent("10001"), "123")]

        self.assertEqual(results, ["当前 UID 未绑定 Emby 用户，请联系管理员执行 /emby add"])

    async def test_search_tool_surfaces_binding_error(self):
        tool = next(item for item in self.context._tools if item.name == "search_emby_media")

        class _ToolContext:
            def __init__(self, event):
                self.context = types.SimpleNamespace(event=event)

        async def _api_request(*_args, **_kwargs):
            return {"error": "当前 UID 未绑定 Emby 用户，请联系管理员执行 /emby add"}

        with patch.object(self.plugin, "api_request", side_effect=_api_request), patch.object(
            self.plugin, "_get_server_id", return_value="server-1"
        ):
            payload = await tool.call(_ToolContext(_DummyEvent("10001")), keyword="test")

        self.assertEqual(
            json.loads(payload),
            {
                "error": "当前 UID 未绑定 Emby 用户，请联系管理员执行 /emby add",
                "emby_server_address": "http://emby.test",
                "emby_server_id": "server-1",
            },
        )


if __name__ == "__main__":
    unittest.main()
