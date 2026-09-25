# QuickTunnel ⇅

零配置、一键式内网穿透桌面工具。基于 Cloudflare Tunnel 官方内核 `cloudflared` 二次封装,无需注册账号、无需命令行,点一下就能把你本机的服务暴露到公网。

## 特性

- **零配置**:无需 Cloudflare 账号,启动即生成临时公网地址
- **桌面 GUI**:CustomTkinter 深色现代界面,状态一目了然
- **多端口映射**:同时暴露多个本地端口,每个端口可映射自定义路径
- **内置反向代理**:一个公网地址 + 路径区分多个服务,自动生成服务目录页与友好错误页
- **离线安装**:内置各平台 cloudflared 内核安装包(xz 高压缩),无网络也能完成初始化;本地包缺失时自动回退在线下载(支持国内镜像加速)
- **断线自动重连**:指数退避重连,网络抖动无感
- **双模式**:GUI 桌面端 / CLI 直连命令行

## 平台支持

| 平台 | 架构 | 离线内核 |
|------|------|----------|
| macOS | Apple Silicon (arm64) | ✅ 内置 |
| Windows | x64 | ✅ 内置 |
| macOS Intel / Linux | amd64 | 从源码运行,内核在线下载 |

## 快速开始

### 方式一:下载打包好的桌面应用(推荐)

到 [Releases](https://github.com/linhj888/quickTunnel/releases) 下载对应平台的压缩包,**每个包只包含该平台的内核**,解压即用、无需安装 Python:

| 文件 | 平台 | 使用方式 |
|------|------|----------|
| `QuickTunnel-v*-macOS-arm64.zip` | macOS Apple Silicon (M1/M2/M3/M4) | 解压得到 `QuickTunnel.app`,双击运行;首次打开如提示未签名,到「系统设置 → 隐私与安全性」点「仍要打开」 |
| `QuickTunnel-v*-Windows-x64.zip` | Windows x64 | 解压后运行 `QuickTunnel/QuickTunnel.exe` |

### 方式二:从源码运行

#### 1. 安装依赖

```bash
pip install customtkinter
```

> Windows / macOS 官方 Python 均自带 tkinter;部分 Linux 发行版需要先安装 `python3-tk`。

#### 2. 启动

```bash
# 桌面 GUI(省略参数即进入 GUI)
python3 main.py

# CLI 直连模式:穿透单个端口
python3 main.py 3000

# CLI 直连模式:穿透一个本地 URL
python3 main.py http://localhost:8080/app
```

#### 3. 使用

1. 在「端口映射」中添加本地服务端口(如 `3000`),路径可留空自动生成
2. 点击「▶ 启动隧道」,几秒后得到公网地址
3. 把地址分享给别人即可;用完点「■ 停止隧道」

## 环境变量

| 变量 | 说明 |
|------|------|
| `QUICKTUNNEL_CF_PATH` | 指定本地 cloudflared 内核路径,跳过自动安装 |
| `QUICKTUNNEL_MIRROR` | 自定义 GitHub 下载镜像前缀,如 `https://gh-proxy.com/%7Burl%7D` |
| `QUICKTUNNEL_PROXY` | 指定 HTTP 代理,如 `http://127.0.0.1:7897` |

## 配置与数据

所有数据存放在 `~/.quicktunnel/`:

- `config.json` — 端口映射配置(自动持久化)
- `bin/` — 自动安装的 cloudflared 内核

## 安全提示

⚠️ 公网 URL 任何拿到链接的人都可以访问,请注意:

- 不要暴露含敏感信息的服务(数据库、管理后台等)
- 使用完毕及时关闭隧道
- 反向代理仅转发你明确添加的端口

## 项目结构

```
quickTunnel/
├── main.py        # 全部核心逻辑(GUI / CLI / 反向代理 / 内核管理)
├── demo_server.py # 9999 端口演示服务,用于测试穿透
└── pkg/           # 各平台离线内核安装包(xz 压缩)
```

## 许可证

[MIT](LICENSE)
