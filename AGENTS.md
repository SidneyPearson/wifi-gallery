# Wi-Fi Gallery

这是 Windows / macOS 均可使用的局域网只读相册。保留现有手机界面、HTTP 路由和登录行为，每台电脑独立选择媒体目录。

- Python 3.10+；依赖见 `requirements.txt`。Windows 入口为 `setup.bat`、`add-folder.bat`、`start.bat`、`stop.bat`，通用命令入口为 `run.bat`。
- 不要把媒体路径写死在代码中。`config.json`、媒体原文件、缓存、日志、PID 和登录密钥不进入版本控制。
- 修改配置不会修改或删除媒体原文件。目录边界使用 `os.path.commonpath`，兼容 Windows 盘符和磁盘根目录。
- Windows 上禁止使用 `os.kill(pid, 0)` 查询进程状态，它会终止进程；使用 `process_running`。
- 开发时使用临时目录配合 `--app-dir` 验证服务，不要停止或改写用户正在使用的默认服务。
- 验证命令：`python -m unittest discover -s tests -v`。跨平台行为由 Windows GitHub Actions 验证。
