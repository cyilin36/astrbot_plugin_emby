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

# --- 1. LLM 函数工具定义 ---

@pydantic_dataclass
class EmbySearchTool(FunctionTool[AstrAgentContext]):
    name: str = "search_emby_media"
    description: str = "搜索 Emby 库中的电影 or 剧集。返回结果包含媒体名称、ID和服务器地址。"
    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {"keyword": {"type": "string", "description": "搜索关键词"}},
        "required": ["keyword"]
    })
    plugin: Any = None
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        host, _, slimit, _ = self.plugin._get_config_safe()
        res = await self.plugin.api_request("Items", {"SearchTerm": kwargs.get("keyword"), "Recursive": True, "Limit": slimit}, context.context.event)
        sid = await self.plugin._get_server_id()
        return json.dumps({"results": res, "emby_server_address": host, "emby_server_id": sid}, ensure_ascii=False)

@pydantic_dataclass
class EmbyLatestTool(FunctionTool[AstrAgentContext]):
    name: str = "get_emby_latest"
    description: str = "查询 Emby 库中最近新上架的影视资源。返回结果包含影片列表、服务器地址和服务器ID。"
    parameters: dict = Field(default_factory=lambda: {"type": "object", "properties": {}})
    plugin: Any = None
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        host, _, _, llimit = self.plugin._get_config_safe()
        res = await self.plugin.api_request("Items", {"SortBy": "DateCreated", "SortOrder": "Descending", "IncludeItemTypes": "Movie,Episode", "Recursive": True, "Limit": llimit}, context.context.event)
        sid = await self.plugin._get_server_id()
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
    async def emby_search(self, event: AstrMessageEvent, keyword: str, limit: int = None):
        '''搜索影片：/emby search <关键词> [数量]'''
        _, _, slimit, _ = self._get_config_safe()
        final_limit = limit if limit is not None else slimit
        res = await self.api_request("Items", {"SearchTerm": keyword, "Recursive": True, "Limit": final_limit}, event)
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
    async def emby_latest(self, event: AstrMessageEvent, limit: int = None):
        '''最近上架：/emby latest [数量]'''
        _, _, _, llimit = self._get_config_safe()
        final_limit = limit if limit is not None else llimit
        res = await self.api_request("Items", {"SortBy": "DateCreated", "SortOrder": "Descending", "IncludeItemTypes": "Movie,Episode", "Recursive": True, "Limit": final_limit}, event)
        items = res.get("Items", [])
        if not items:
            yield event.plain_result("获取最新失败")
            return
        out = [f"最近上架/更新 (展示 {len(items)} 条):"]
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