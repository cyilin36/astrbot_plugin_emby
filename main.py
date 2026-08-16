import json
import os
import httpx
from typing import Any
from pydantic import Field
from pydantic.dataclasses import dataclass as pydantic_dataclass

from astrbot.api import logger
from astrbot.api.all import *
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext

LATEST_MEDIA_TYPE_MAP = {
    "全部": "Movie,Episode,Audio,Video,Photo",
    "电影": "Movie",
    "电视剧": "Episode",
    "音乐": "Audio",
    "家庭视频": "Video",
    "照片": "Photo",
}

LATEST_MEDIA_TYPE_LABELS = " / ".join(LATEST_MEDIA_TYPE_MAP.keys())
USER_SCOPED_MEDIA_ENDPOINTS = ("Items",)
WATCHED_FILTER_MAP = {
    "全部": None,
    "已看": "IsPlayed",
    "未看": "IsUnplayed",
}

WATCHED_FILTER_LABELS = " / ".join(WATCHED_FILTER_MAP.keys())

# --- 1. LLM 函数工具定义 ---

@pydantic_dataclass
class EmbySearchTool(FunctionTool[AstrAgentContext]):
    name: str = "search_emby_media"
    description: str = f"搜索 Emby 库中的电影 or 剧集，支持按观看状态过滤（{WATCHED_FILTER_LABELS}）。返回结果包含媒体名称、ID和服务器地址。"
    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "keyword": {"type": "string", "description": "搜索关键词"},
            "watched": {
                "type": "string",
                "enum": list(WATCHED_FILTER_MAP.keys()),
                "description": f"观看状态过滤，可选：{WATCHED_FILTER_LABELS}。默认是全部。",
            },
        },
        "required": ["keyword"],
    })
    plugin: Any = None
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        host, _, slimit, _ = self.plugin._get_config_safe()
        watched, error = self.plugin._normalize_watched_filter(kwargs.get("watched"))
        if error:
            return json.dumps({"error": error}, ensure_ascii=False)
        params = {"SearchTerm": kwargs.get("keyword"), "Recursive": True, "Limit": slimit}
        if watched:
            params["Filters"] = watched
        res = await self.plugin.api_request("Items", params, context.context.event)
        sid = await self.plugin._get_server_id()
        if "error" in res:
            return json.dumps({"error": res["error"], "emby_server_address": host, "emby_server_id": sid}, ensure_ascii=False)
        return json.dumps({"results": res, "emby_server_address": host, "emby_server_id": sid}, ensure_ascii=False)

@pydantic_dataclass
class EmbyLatestTool(FunctionTool[AstrAgentContext]):
    name: str = "get_emby_latest"
    description: str = f"查询 Emby 库中最近上新的媒体条目。支持类型：{LATEST_MEDIA_TYPE_LABELS}。返回结果包含条目列表、服务器地址和服务器ID。"
    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "media_type": {
                "type": "string",
                "description": f"可选媒体类型：{LATEST_MEDIA_TYPE_LABELS}。默认是全部。",
            }
        },
    })
    plugin: Any = None
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        host, _, _, llimit = self.plugin._get_config_safe()
        media_type, error = self.plugin._normalize_latest_media_type(kwargs.get("media_type"))
        if error:
            return json.dumps({"error": error}, ensure_ascii=False)
        res = await self.plugin.api_request(
            "Items",
            self.plugin._build_latest_query_params(llimit, media_type),
            context.context.event,
        )
        sid = await self.plugin._get_server_id()
        if "error" in res:
            return json.dumps({"error": res["error"], "emby_server_address": host, "emby_server_id": sid}, ensure_ascii=False)
        return json.dumps({"results": res, "emby_server_address": host, "emby_server_id": sid}, ensure_ascii=False)

@pydantic_dataclass
class EmbyDetailTool(FunctionTool[AstrAgentContext]):
    name: str = "get_emby_detail"
    description: str = "通过媒体 ID 获取详细信息。返回结果包含详情、服务器地址和服务器ID。"
    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {"item_id": {"type": "string", "description": "媒体 ID"}},
        "required": ["item_id"]
    })
    plugin: Any = None
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        res = await self.plugin.api_request(f"Items/{kwargs.get('item_id')}", {}, context.context.event)
        host, _, _, _ = self.plugin._get_config_safe()
        sid = await self.plugin._get_server_id()
        if "error" in res:
            return json.dumps({"error": res["error"], "emby_server_address": host, "emby_server_id": sid}, ensure_ascii=False)
        return json.dumps({"detail": res, "emby_server_address": host, "emby_server_id": sid}, ensure_ascii=False)

# --- 2. 插件主类 ---

@register("astrbot_plugin_emby", "Gemini", "Emby 助手", "13.0.0")
class EmbyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = os.path.join("data", "plugins", "astrbot_plugin_emby")
        self.local_conf_file = os.path.join(self.data_dir, "config.json")
        self.binding_file = os.path.join(self.data_dir, "user_bindings.json")
        if not os.path.exists(self.data_dir): os.makedirs(self.data_dir, exist_ok=True)

        self.context.add_llm_tools(EmbySearchTool(plugin=self))
        self.context.add_llm_tools(EmbyLatestTool(plugin=self))
        self.context.add_llm_tools(EmbyDetailTool(plugin=self))

    def _get_config_safe(self):
        # 使用框架注入的 config 对象（对应 WebUI 配置面板）
        host = self.config.get("emby_host", "").strip()
        key = self.config.get("api_key", "").strip()
        search_limit = self.config.get("search_limit", 10)
        latest_limit = self.config.get("latest_limit", 10)
        
        # 如果用户没填协议头，默认使用 http
        if host and not host.startswith("http"):
            host = f"http://{host}"
        
        return host, key, search_limit, latest_limit

    def _normalize_latest_media_type(self, media_type: str | None):
        label = (media_type or "全部").strip()
        if label not in LATEST_MEDIA_TYPE_MAP:
            return None, f"不支持的类型，可选：{LATEST_MEDIA_TYPE_LABELS}"
        return label, None

    def _normalize_watched_filter(self, watched: str | None):
        label = (watched or "全部").strip()
        if label not in WATCHED_FILTER_MAP:
            return None, f"不支持的观看状态，可选：{WATCHED_FILTER_LABELS}"
        return WATCHED_FILTER_MAP[label], None

    def _build_latest_query_params(self, limit: int, media_type: str):
        params = {
            "SortBy": "DateCreated",
            "SortOrder": "Descending",
            "Recursive": True,
            "Limit": limit,
            "IncludeItemTypes": LATEST_MEDIA_TYPE_MAP[media_type],
        }
        return params

    def _parse_latest_command_args(self, first_arg: str | None, second_arg: str | None, default_limit: int):
        media_type = None
        limit = default_limit

        for raw_arg in (first_arg, second_arg):
            if raw_arg is None:
                continue
            arg = raw_arg.strip()
            if not arg:
                continue
            if arg.isdigit():
                limit = int(arg)
                continue
            if media_type is not None:
                return None, None, f"参数格式错误，支持：/emby latest [类型] [数量]"
            media_type = arg

        media_type, error = self._normalize_latest_media_type(media_type)
        if error:
            return None, None, error

        return media_type, limit, None

    def _parse_search_command_args(self, first_arg: str | None, second_arg: str | None, default_limit: int):
        watched_label = "全部"
        limit = default_limit
        watched_set = False

        for raw_arg in (first_arg, second_arg):
            if raw_arg is None:
                continue
            arg = raw_arg.strip()
            if not arg:
                continue
            if arg.isdigit():
                limit = int(arg)
                continue
            if watched_set or arg not in WATCHED_FILTER_MAP:
                return None, None, f"参数格式错误，支持：/emby search <关键词> [全部/已看/未看] [数量]"
            watched_label = arg
            watched_set = True

        watched, error = self._normalize_watched_filter(watched_label)
        if error:
            return None, None, error

        return watched, limit, None

    def _get_bindings(self):
        # 回归到独立文件管理
        if os.path.exists(self.binding_file):
            try:
                with open(self.binding_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except: pass
        return {}

    def _save_bindings(self, bindings):
        try:
            with open(self.binding_file, 'w', encoding='utf-8') as f:
                json.dump(bindings, f, indent=4, ensure_ascii=False)
        except: pass

    def _requires_user_binding(self, endpoint: str) -> bool:
        normalized_endpoint = endpoint.lstrip('/')
        return any(
            normalized_endpoint == scoped
            or normalized_endpoint.startswith(f"{scoped}/")
            for scoped in USER_SCOPED_MEDIA_ENDPOINTS
        )

    async def _get_server_id(self):
        if hasattr(self, "_cached_server_id") and self._cached_server_id:
            return self._cached_server_id
        host, key, _, _ = self._get_config_safe()
        if not host: return None
        url = f"{host.rstrip('/')}/emby/System/Info"
        async with httpx.AsyncClient(verify=False, timeout=5.0) as client:
            try:
                resp = await client.get(url, headers={"X-Emby-Token": key})
                self._cached_server_id = resp.json().get("Id")
                return self._cached_server_id
            except: return None

    async def api_request(self, endpoint: str, params: dict, event: AstrMessageEvent = None):
        host, key, _, _ = self._get_config_safe()
        if not host: return {"error": "配置缺失"}
        host = host.rstrip('/')
        requires_user_binding = self._requires_user_binding(endpoint)
        
        uid = None
        if event:
            try:
                bindings = self._get_bindings()
                val = bindings.get(str(event.get_sender_id()))
                if isinstance(val, dict):
                    uid = val.get("id")
                else:
                    uid = val
            except: pass

        if requires_user_binding and not uid:
            return {"error": "当前 UID 未绑定 Emby 用户，请联系管理员执行 /emby add"}

        if uid and not any(k in endpoint for k in ["Users", "System", "Public", "Sessions", "Library"]):
            url = f"{host}/emby/Users/{uid}/{endpoint.lstrip('/')}"
        else:
            url = f"{host}/emby/{endpoint.lstrip('/')}"

        async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
            try:
                resp = await client.get(url, headers={"X-Emby-Token": key, "Accept": "application/json"}, params=params)
                return resp.json()
            except Exception as e: return {"error": str(e)}

    # --- 3. 指令组 ---

    @filter.command_group("emby")
    def emby(self):
        """Emby 指令组"""
        pass

    @emby.command("search")
    async def emby_search(self, event: AstrMessageEvent, keyword: str, first_arg: str = None, second_arg: str = None):
        '''搜索影片：/emby search <关键词> [全部/已看/未看] [数量]'''
        _, _, slimit, _ = self._get_config_safe()
        watched, final_limit, error = self._parse_search_command_args(first_arg, second_arg, slimit)
        if error:
            yield event.plain_result(error)
            return
        params = {"SearchTerm": keyword, "Recursive": True, "Limit": final_limit}
        if watched:
            params["Filters"] = watched
        res = await self.api_request("Items", params, event)
        if "error" in res:
            yield event.plain_result(res["error"])
            return
        items = res.get("Items", [])
        if not items:
            yield event.plain_result(f"未找到与 '{keyword}' 相关的结果")
            return
        out = [f"搜索 '{keyword}' 的结果 (展示 {len(items)} 条):"]
        for i in items:
            year = i.get('ProductionYear')
            year_str = f" [{year}]" if year else ""
            out.append(f"- {i.get('Name')}{year_str} (ID: {i.get('Id')})")
        yield event.plain_result("\n".join(out))

    @emby.command("latest")
    async def emby_latest(self, event: AstrMessageEvent, first_arg: str = None, second_arg: str = None):
        '''最近上新：/emby latest [类型] [数量]'''
        _, _, _, llimit = self._get_config_safe()
        media_type, final_limit, error = self._parse_latest_command_args(first_arg, second_arg, llimit)
        if error:
            yield event.plain_result(error)
            return

        res = await self.api_request("Items", self._build_latest_query_params(final_limit, media_type), event)
        if "error" in res:
            yield event.plain_result(res["error"])
            return
        items = res.get("Items", [])
        if not items:
            yield event.plain_result("获取最新失败")
            return
        out = [f"最近上新-{media_type} (展示 {len(items)} 条):"]
        for i in items:
            name = i.get('Name')
            # 如果是单集，尝试获取剧名和季度集数
            if i.get('Type') == 'Episode':
                series = i.get('SeriesName', '未知剧集')
                season = i.get('ParentIndexNumber', 0)
                episode = i.get('IndexNumber', 0)
                name = f"{series} - S{season:02d}E{episode:02d} - {name}"
            
            year = i.get('ProductionYear')
            year_str = f" [{year}]" if year else ""
            out.append(f"- {name}{year_str} (ID: {i.get('Id')})")
        yield event.plain_result("\n".join(out))

    @emby.command("detail")
    async def emby_detail(self, event: AstrMessageEvent, item_id: str):
        '''查看详情：/emby detail <ID>'''
        res = await self.api_request(f"Items/{item_id}", {}, event)
        if "error" in res:
            yield event.plain_result(res["error"])
            return
        if "Name" in res:
            year = res.get('ProductionYear')
            year_val = year if year else "未知"
            msg = [
                f"名称: {res.get('Name')}",
                f"年份: {year_val}",
                f"评分: {res.get('CommunityRating', '无')}",
                f"简介: {res.get('Overview', '暂无简介')[:150]}..."
            ]
            yield event.plain_result("\n".join(msg))
        else:
            yield event.plain_result(f"未找到 ID 为 {item_id} 的影片")

    # --- 4. 管理员指令 ---

    @emby.command("add")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def emby_add(self, event: AstrMessageEvent, target_uid: str, emby_username: str):
        '''管理员添加绑定：/emby add <UID> <用户名>'''
        host, key, _, _ = self._get_config_safe()
        
        # 1. 检查 UID 是否已经绑定
        bindings = self._get_bindings()
        
        if str(target_uid) in bindings:
            # 需要先获取 Emby 用户名来给出友好提示，这里直接请求 Emby 用户列表
            async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
                try:
                    resp = await client.get(f"{host.rstrip('/')}/emby/Users", headers={"X-Emby-Token": key})
                    users = resp.json()
                    val = bindings[str(target_uid)]
                    current_eid = val.get("id") if isinstance(val, dict) else val
                    current_ename = next((u.get('Name') for u in users if u.get('Id') == current_eid), "未知账号")
                    yield event.plain_result(f"该 UID 已绑定到 Emby 用户: {current_ename}，请先执行解绑操作")
                    return
                except:
                    yield event.plain_result(f"该 UID 已存在绑定记录，请先解绑")
                    return

        # 2. 正常绑定流程
        async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
            try:
                if not host.startswith("http"): host = f"http://{host}"
                resp = await client.get(f"{host.rstrip('/')}/emby/Users", headers={"X-Emby-Token": key})
                users = resp.json()
                target = next((u for u in users if u.get('Name') == emby_username), None)
                if not target:
                    yield event.plain_result(f"未找到 Emby 用户: {emby_username}")
                    return
                
                bindings[str(target_uid)] = {"id": target['Id'], "name": emby_username}
                self._save_bindings(bindings)
                yield event.plain_result(f"绑定成功: {target_uid} -> {emby_username}")
            except Exception as e: yield event.plain_result(f"错误: {str(e)}")

    @emby.command("rm")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def emby_rm(self, event: AstrMessageEvent, target_uid: str):
        '''管理员解绑：/emby rm <UID>'''
        bindings = self._get_bindings()
        if str(target_uid) in bindings:
            del bindings[str(target_uid)]
            self._save_bindings(bindings)
            yield event.plain_result("已解绑")
        else: yield event.plain_result("未找到记录")

    @emby.command("ls")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def emby_ls(self, event: AstrMessageEvent):
        '''管理员列出绑定：/emby ls'''
        bindings = self._get_bindings()
        if not bindings: yield event.plain_result("列表为空"); return
        res = ["绑定列表:"]
        for uid, val in bindings.items():
            if isinstance(val, dict):
                res.append(f"UID: {uid} -> Emby: {val.get('name')} ({val.get('id')})")
            else:
                res.append(f"UID: {uid} -> EmbyID: {val}")
        yield event.plain_result("\n".join(res))

    @emby.command("status")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def emby_status(self, event: AstrMessageEvent):
        '''管理员查询状态：/emby status'''
        host, key, _, _ = self._get_config_safe()
        if not host: yield event.plain_result("配置缺失"); return
        
        async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
            try:
                base_url = host.rstrip('/')
                headers = {"X-Emby-Token": key}
                
                sys_res = await client.get(f"{base_url}/emby/System/Info", headers=headers)
                sys_data = sys_res.json()
                
                lib_res = await client.get(f"{base_url}/emby/Library/VirtualFolders", headers=headers)
                libs = lib_res.json()
                type_map = {"movies": "电影", "tvshows": "剧集", "music": "音乐", "boxsets": "合集", "folders": "文件夹"}
                
                session_res = await client.get(f"{base_url}/emby/Sessions", headers=headers)
                active_watch = []
                for s in session_res.json():
                    if "NowPlayingItem" in s:
                        item = s['NowPlayingItem']
                        title = f"{item.get('SeriesName', '')} - {item.get('Name')}" if 'SeriesName' in item else item.get('Name')
                        active_watch.append(f"- {s.get('UserName')} 正在看：{title}")

                msg = [
                    f"Emby 服务器在线",
                    f"地址: {host}",
                    f"名称: {sys_data.get('ServerName')}",
                    f"版本: {sys_data.get('Version')}",
                    f"\n媒体库详情:"
                ]
                
                for lib in libs:
                    c_type = lib.get('CollectionType', 'folders')
                    msg.append(f"- {lib.get('Name')} [{type_map.get(c_type, '其他')}]")
                
                msg.append(f"\n实时活动:")
                msg.extend(active_watch if active_watch else ["- 当前无观影活动"])
                
                yield event.plain_result("\n".join(msg))
            except Exception as e: yield event.plain_result(f"查询失败: {str(e)}")
