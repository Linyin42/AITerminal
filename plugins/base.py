"""
插件基类 - 定义 AITerminal 插件的标准接口

插件放置目录: <项目根>/plugins/
插件文件命名: plugin_xxx.py，其中需包含一个 Plugin 类（继承 PluginBase）

插件生命周期:
    on_load(app)      程序启动、加载插件时调用
    on_session_open(session)   新建会话后调用
    on_output(session, text)   收到设备输出时调用（返回 False 可阻止显示）
    on_command(session, command)  命令下发前调用（返回 False 可阻止下发）
    on_unload()       程序退出时调用
"""
from typing import Any, Optional


class PluginBase:
    """所有插件的基类"""

    #: 插件名称（显示在插件列表中）
    name = "unnamed-plugin"
    #: 插件版本
    version = "0.0.1"
    #: 作者
    author = ""
    #: 功能说明
    description = ""
    #: 是否需要 UI 扩展（预留）
    has_ui = False

    def __init__(self):
        self.app: Optional[Any] = None
        self.enabled = True

    # ------------------------------------------------------------ 生命周期
    def on_load(self, app):
        """插件被加载时调用，app 为主窗口实例"""
        self.app = app

    def on_session_open(self, session):
        """新会话建立后调用"""
        return None

    def on_session_close(self, session):
        """会话关闭时调用"""
        return None

    def on_output(self, session, text: str) -> bool:
        """
        收到设备输出时调用

        返回 False 表示拦截该输出（不再显示给用户）
        """
        return True

    def on_command(self, session, command: str) -> bool:
        """
        命令下发前调用

        返回 False 表示拦截该命令（不发送到设备）
        """
        return True

    def on_unload(self):
        """插件卸载/程序退出时调用"""
        return None

    # ------------------------------------------------------------ 工具
    def log(self, message: str):
        """输出插件日志"""
        print(f"[plugin:{self.name}] {message}")

    def __repr__(self):
        return f"<Plugin {self.name} v{self.version}>"


class PluginManager:
    """
    插件管理器

    扫描 plugins/ 目录下的 plugin_*.py，加载其中的 Plugin 类。
    """

    def __init__(self, plugins_dir: str = None):
        self.plugins = []
        self.plugins_dir = plugins_dir

    def load_all(self, app=None):
        """加载全部插件"""
        import os
        import importlib.util

        if not self.plugins_dir or not os.path.isdir(self.plugins_dir):
            return self.plugins

        for filename in sorted(os.listdir(self.plugins_dir)):
            if not (filename.startswith("plugin_") and filename.endswith(".py")):
                continue
            path = os.path.join(self.plugins_dir, filename)
            try:
                spec = importlib.util.spec_from_file_location(filename[:-3], path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                plugin_cls = getattr(module, "Plugin", None)
                if plugin_cls is None:
                    continue
                plugin = plugin_cls()
                if not isinstance(plugin, PluginBase):
                    continue
                if app is not None:
                    plugin.on_load(app)
                self.plugins.append(plugin)
            except Exception as e:
                print(f"[plugin] 加载 {filename} 失败: {e}")
        return self.plugins

    def dispatch(self, hook: str, *args) -> bool:
        """广播钩子；任一插件返回 False 则整体返回 False"""
        allow = True
        for plugin in self.plugins:
            if not plugin.enabled:
                continue
            func = getattr(plugin, hook, None)
            if func is None:
                continue
            try:
                if func(*args) is False:
                    allow = False
            except Exception as e:
                print(f"[plugin] {plugin.name}.{hook} 异常: {e}")
        return allow

    def unload_all(self):
        for plugin in self.plugins:
            try:
                plugin.on_unload()
            except Exception:
                pass
        self.plugins.clear()
