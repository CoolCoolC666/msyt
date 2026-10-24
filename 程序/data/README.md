# `程序/data/` —— 运行时数据与本机配置

这个目录里放的是**运行时数据**和**只有自己机器上才有效的东西**。
仓库里**只有本文件**会被提交，其余全部被 `.gitignore` 挡掉。

> 想从零跑起来：**这个目录不用手动建**，程序会自己建。

---

## 一、哪些是自动生成的，哪些要自己填

| 文件 / 目录 | 谁创建 | 内容 | 提交？ |
|---|---|---|---|
| `msyt.db`（及 `-wal` / `-shm`） | 程序自动 | SQLite 主库：帖、楼层、受理流程、标签、队列、协作记录 | ❌ 绝不入库 |
| `exports/` | 程序自动 | `msyt export` 的 CSV 输出目录 | ❌ 绝不入库 |
| `admin-账号.txt` | 首次启动自动 | 管理员初始账号与初始密码 | ❌ 绝不入库 |
| `web.pid` / `web.tunnel.pid` | 启停脚本自动 | 进程号，用来精确停止服务 | ❌ 绝不入库 |
| `*.log` | 运行时自动 | 服务与隧道的输出日志 | ❌ 绝不入库 |
| `tunnel-domain.txt` | **自己填** | 你要挂的公网域名（如 `msyt.example.com`） | ❌ 绝不入库 |
| `agent-api-base.txt` | **自己填** | agent 的**备用**控制面地址 | ❌ 绝不入库 |
| `cloudflared-tunnel.token` | **自己放** | Cloudflare 隧道令牌 | ❌ 绝不入库 |
| `tunnel-protocol.txt` | 可选 | `quic` / `http2` / `auto` | ✅ 可提交 |
| `cf-service-token.txt` | 不用手填 | 仅当用 Cloudflare API 自动改 Access 策略时才需要 | ❌ 绝不入库 |

`程序/config.json` 同理：**不填也能跑**（全部走默认值），
一旦你改了配置想提交，请先确认里面没有本机绝对路径。

---

## 二、只想本地跑（最简）

什么都不用填。直接：

```powershell
cd 程序
python -m msyt web          # → http://127.0.0.1:8765/
```

管理员初始账号会写进 `admin-账号.txt`，**登录后第一件事是改密码**
（「协作 → 我的账号」里能改，管理员重置密码只是给一个临时口令，
成员登录后必须能自己改掉）。

---

## 三、要挂到公网给小组成员用（可选）

三步，缺一不可。

### 1. Cloudflare 侧建隧道

```powershell
cloudflared tunnel create msyt-local
```

把命令输出的令牌存进本目录：

```
程序\data\cloudflared-tunnel.token
```

### 2. 填域名

```
程序\data\tunnel-domain.txt
```

内容就是你的域名，一行，例如：

```
msyt.example.com
```

> 启动器会**自动读这个文件**，域名换成别的**不用改脚本**。
> 域名建议用**同级**子域名（`msyt.example.com` / `agentapi.example.com`），
> 两级子域（`api.msyt.example.com`）的证书可能一直签不下来。

### 3. 启停

```powershell
.\web.ps1 start -Tunnel      # 起服务 + 起隧道
.\web.ps1 status             # 看状态
.\web.ps1 stop               # 停服务（隧道一起停）
```

### 隧道不通时

| 现象 | 多半是 |
|---|---|
| 本地 `127.0.0.1:8765` 正常，公网一直 **502** | 隧道没连上，或后端服务没起 —— **先看隧道日志里的 dial 错误** |
| 隧道日志报 `failed to dial to edge with quic: timeout` | 本机网络不通 UDP/QUIC → 在 `tunnel-protocol.txt` 写 `http2`（走 TCP），通常立刻通 |
| 公网访问被弹到 `cloudflareaccess.com/.../login` | 这是 **Access 门**在起作用，不是故障 |
| 改了 `tunnel-domain.txt` 但地址没变 | 域名做成配置就是为了免改脚本；确认改的是**这个文件**且已重启服务 |

---

## 四、agent 的备用地址

`agent-api-base.txt` 填的是**备用**控制面地址。启动包会同时写入**主地址**与**备用地址**，
agent 启动时**自己探测**哪个通（401 也算「通」）——这样主备任一挂了都不会卡住成员。

- 成员在**本地**下载包 → 主地址是本地下载用的地址，备用是这里配的；
- 成员在**公网**下载包 → 主地址是这里配的，备用是公网访问地址。

没填也能用（只有主地址）。

---

## 五、别把这些提交进仓库

- 管理员初始密码（`admin-账号.txt`）
- 隧道令牌（`cloudflared-tunnel.token`）—— 拿到它就等于拿到你的入口
- 数据库（`msyt.db`）—— 含站方内容与全部成员信息
- 成员名单、导出 CSV、运行日志

`.gitignore` 已经挡住这些，但**提交前自己再扫一眼**
（`git status` 里出现上面这些名字就说明规则漏了）。
