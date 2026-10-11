# Wi-Fi 相册

在 Windows 或 Mac 上运行，手机连接同一 Wi-Fi 后，通过浏览器查看电脑里的照片和视频。每台电脑自行选择媒体目录，可以随时增删；程序不绑定任何预设目录。

## 功能

- 目录首页显示媒体数量，支持逐层浏览和跨目录「全部媒体」。
- 图片与视频缩略图、图片放大、左右滑动切换、视频播放与拖动进度。
- 适合 iPhone 的苹果风格界面，可以添加到主屏幕。
- 登录后记住 90 天；修改密码后旧登录立即失效。
- 媒体原文件只读；手机只能访问主动添加的目录。
- 添加、移除目录立即生效，无需重启。

## Windows 开始使用

需要 Python 3.10 或更高版本，安装时勾选 **Add Python to PATH**。

1. 登录 GitHub，下载或克隆这个项目，在 Windows Codex 中打开。
2. 双击 `setup.bat`，安装运行依赖。图片、HEIC 和视频缩略图的依赖会一起安装。
3. 双击 `add-folder.bat`，在弹出的窗口中选择你希望浏览的媒体目录。可以重复添加多个目录。
4. 双击 `start.bat`，窗口会显示手机访问地址、用户名和随机生成的密码。
5. 手机连接同一 Wi-Fi，在浏览器中打开显示的地址并登录。

停止服务时双击 `stop.bat`。电脑重启后，再次双击 `start.bat` 即可。

系统防火墙首次提示时，允许相册在**专用网络**上通信。视频能否播放取决于手机浏览器对原视频编码的支持，程序不会转码或改动原视频。

其他操作可在项目目录的 PowerShell / 命令提示符中执行：

```powershell
.\run.bat list
.\run.bat add "D:\Photos" --name "我的照片"
.\run.bat remove 0
.\run.bat status
.\run.bat passwd
.\run.bat clear-cache
```

这些路径只是使用示例，请换成你自己的目录。`remove` 只移除相册中的目录登记，原文件保留。

## Mac 开始使用

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python gallery.py choose
.venv/bin/python gallery.py start
```

也可用 `add "目录路径"` 添加目录。其他命令为 `list`、`remove`、`stop`、`status`、`passwd` 和 `clear-cache`。

Mac 保留系统自带的缩略图方式，另有跨平台图片与视频处理作为补充。已有配置继续使用。

## 地址、登录与主屏幕

- 默认地址：`http://电脑的局域网IP:9265`，`start` / `status` 会显示实际地址。
- 默认用户名：`gallery`；首次使用自动生成密码，`start` 会显示。
- 用 `passwd` 生成新随机密码，或 `passwd 新密码` 设置自己的密码。
- 浏览器访问 `/logout` 可以退出。
- iPhone Safari 登录后，点「分享」→「添加到主屏幕」，即可从主屏幕打开相册。

## 每台电脑独立配置

首次运行会自动生成 `config.json`，包含这台电脑的登录信息和媒体目录。仓库提供 `config.example.json` 作为配置格式示例，通常无需手动复制；空密码和空密钥会在首次运行时自动生成。

程序代码和界面保存在 GitHub。媒体文件、媒体目录配置、登录密码、缩略图缓存、日志和进程状态只留在各自电脑上，均已加入 `.gitignore`。

两台电脑可以使用相同功能，同时管理各自选择的内容。后续更新程序时，先停止相册，再执行 `git pull`，更新依赖后重新启动。

## 给 Windows Codex 的接手说明

在 Windows 上打开本项目后，可以直接发送：

> 请先读 README.md 和 AGENTS.md，准备 Python 环境和依赖，让这个 Wi-Fi 相册在我的 Windows 上运行。我会通过目录选择窗口自行添加媒体目录。请保留已有相册功能，验证启动、停止、图片和视频缩略图，最后告诉我手机访问地址。

## 开发验证

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

测试使用临时配置和生成的媒体文件。GitHub Actions 在 Windows 上验证启动、重复启动、状态查询、停止、登录、目录浏览、原文件只读、范围请求和缩略图。
