# AstrBot Emby 助手插件

这是一个为 AstrBot 开发的插件，旨在将你的自部署 Emby 媒体库 接入机器人。它不仅支持通过`/emby`指令进行手动管理，还能让 AI 具备搜索和查询你媒体库的能力。

## 核心功能

- 智能 AI 交互：AI能够直接搜索电影、剧集，并告诉你最近更新了什么。支持自然语言搜索和指令搜索

- 多用户绑定：将UID与Emby用户ID绑定，实现“个人库”视图。

- 状态实时监控：查看服务器版本、媒体库分类以及当前谁正在看什么。

## 安装配置

### 部署插件

将插件文件夹放入AstrBot的`data/plugins/`目录下并重启astrbot，确保目录结构如下：

```
astrbot_plugin_emby/
├── main.py
├── metadata.yaml
├── _conf_schema.json # webui配置面板定义
└── user_bindings.json # 用户信息存储
```

### 配置服务器信息

你可以在 AstrBot 的 **WebUI 插件配置面板** 中直接填写服务器信息。该插件会自动生成配置界面，支持修改：

- **Emby 服务器地址**：你的 Emby 访问 URL。
- **API 密钥**：在 Emby 管理面板生成的密钥。
- **条目数量限制**：设置搜索和最近上架默认展示的数量。

>注意：API 密钥在Emby管理面板 -> 设置 -> API 密钥 中生成。

## 使用操作

- `/emby add/rm/ls/status`分别表示：用户绑定、用户解绑、一览用户绑定信息、服务器状态。是管理员才能使用的指令。
- `/emby search/latest/detail`分别表示：搜索、查看最近更新、查看影片元数据。所有人都可以使用。

### 用户绑定与解绑

使用`/emby add <UID> <Emby用户名>`来绑定信息，实现与Emby账户相同的访问媒体库限制。使用`/emby rm <UID> `来解除绑定。

数据会存储在`user_bindings.json`文件中，包含 Emby ID 和用户名备注，方便管理。

>UID可以使用`/sid`查看。
>一个UID只能绑定一个Emby用户，但一个Emby用户可以绑定多个UID。

### 搜索影片

- 指令搜索：`/emby search <关键词> <搜索数量>`。
- 自然语言：直接说出类似“库里面xxxxxxx”的话。

默认展示数量可在 WebUI 插件配置面板中修改。

### 获取元数据

- 指令搜索：`/emby detail <ID>`。ID是Emby中的`item?id=`，不懂得可以使用`/emby search`获取。
- 自然语言：直接说出来即可。

### 获取最近更新

- 指令操作：`/emby latest <展示数量>`。
- 自然语言：直接说出“最近库里有什么更新的”。

默认展示数量可在 WebUI 插件配置面板中修改。