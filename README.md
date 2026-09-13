# newapi-checkin

[![Release](https://img.shields.io/github/v/release/JINGTU0V0/newapi-checkin?label=release&color=blue)](https://github.com/JINGTU0V0/newapi-checkin/releases/latest)
[![Download exe](https://img.shields.io/github/downloads/JINGTU0V0/newapi-checkin/v1.0.0/total?label=exe%20downloads&color=green)](https://github.com/JINGTU0V0/newapi-checkin/releases/latest)

多站点 New API / One API 公益中转站每日自动签到。纯 HTTP，无浏览器依赖，配置驱动。

从个人用了几个月、覆盖 9+ 站点的实战脚本泛化而来：每类反自动化对策收敛为一个配置字段，
站点、账号、通知全部写在配置里，**加站改不用碰代码**。

## 特性

- ✅ 标准 New/One API 账密登录 + 签到，**签到前预检**（今日已签不再 POST，行为更像人）
- ✅ 五类反自动化模式，按站点声明：
  | 字段 | 场景 |
  |---|---|
  | `claim` | 公益额度领取端点（代替标准签到） |
  | `sign` | timestamp + HmacSHA256 签名（密钥从前端 bundle 里扒） |
  | `proof` | Altcha 式 PoW 验证，解出一次性 proof |
  | `ocr` | 图形验证码，ddddocr 识别重试（需 `pip install ddddocr pillow`） |
  | `mode: routerteam` | JWT 鉴权站（签到 + 每日抽奖报名） |
  | `session_cookie` / `access_token` | 直连凭证：跳过登录接口，绕开 Cloudflare Turnstile 等人机验证（见下文） |
  面板添加站点时点「🔍 自动检测模式」，用你的凭据实测自动选出正确模式并展示判定依据。
- ✅ Telegram 通知：失败当天每站只报一次；当天全部完成后推日报（各站奖励 + 合计美元）
- ✅ `.checkin_ledger.json` 按日记账奖励额度与余额，自动保留 90 天
- ✅ 增量模式 `--today`：状态文件按日记进度，重跑只补失败站——定时一天两次即自动重试
- ✅ 密码支持 `${ENV_VAR}` 占位，配置文件可以放心提交进私有仓库
- ✅ 零依赖偏好：除 `requests` 外全是标准库；没装 PyYAML 时内置受限解析器兜底

## 快速开始

**Windows 零门槛**：到 [Releases](https://github.com/JINGTU0V0/newapi-checkin/releases/latest) 下载
`newapi-checkin.exe` 双击即用（交互菜单含签到 / Web 面板 / 编辑配置 / 日报）。首次运行若
SmartScreen 拦截，点「仍要运行」（未签名二进制）。

```bash
pip install requests
cp sites.example.yaml sites.yaml   # 改成你的站点
cp creds.example.json creds.json   # 填账号（或把密码写成 ${ENV} 占位）
python checkin.py --list           # 检查配置
python checkin.py --notify         # 手动跑一轮
```

## 部署（三选一）

**1. 服务器 systemd**（推荐，一天两次自动补跑）：

```ini
# /etc/systemd/system/checkin.service  Type=oneshot
# ExecStart=/usr/bin/python3 /path/checkin.py --today --notify
# timer: OnCalendar=*-*-* 06:00:00 与 10:30:00 两条，Persistent=true
```

**2. Docker**：

```bash
docker compose up -d --build
```

**3. GitHub Actions**（适合纯 API 站；fork 后填 secrets `TG_BOT_TOKEN` / `TG_CHAT_ID`，
把 sites.yaml、creds.json 提交到你自己的私有 fork，或继续全用 `${ENV}` 占位+secrets）：
启用仓库的 Actions 即可，每天北京时间 06:00 / 10:30 各跑一次。

**4. Windows exe**（给不想装 Python 的人）：

自己构建（需 [uv](https://docs.astral.sh/uv/)，约 10MB，无安装、拷到哪配到哪）：

```bash
uv run --no-project --python 3.12 --with pyinstaller --with requests \
  python -m PyInstaller --onefile --name newapi-checkin --console \
  --add-data "sites.example.yaml;." checkin.py
# 产物 dist/newapi-checkin.exe
```

双击 exe 进交互菜单：首次运行自动在 exe 旁生成 sites.yaml / creds.json 模板并引导填
用户名；菜单可立即签到、补跑、打开配置文件编辑、常驻定时（06:00/10:30）、打开 Web 面板。
命令行用法不变（`newapi-checkin.exe --today --notify`），也可以直接扔进 Windows 计划任务。

> 首次双击可能被 SmartScreen 拦截（无代码签名），点「仍要运行」即可；介意可自行签名。

## Web 面板（`--ui`）

不想碰命令行/记事本的人可以开图形界面：

```bash
python checkin.py --ui                # 源码运行
newapi-checkin.exe --ui               # exe（或菜单里选 6）
python checkin.py --ui --ui-host 0.0.0.0 --ui-token 你的token   # 局域网访问必须带 token
```

单页面板含：站点状态卡片、一键签到/重跑失败站（实时日志流）、90 天收益曲线（日收益柱 +
累计曲线）、sites.yaml / creds.json 在线编辑（保存前做语法校验，坏配置直接拒绝）。
**添加网站走表单**：卡片上的「＋ 添加网站 / 编辑」弹窗填名称、地址、用户名/邮箱、密码、
签到模式（含 claim/sign/proof/ocr/routerteam 六种），密码留 `***` 表示不修改；站点块直接
写回 sites.yaml（高级字段如 claim 配置、注释原样保留），账号自动同步 creds.json。
**模式可自动检测**：填好地址和账号后点「🔍 自动检测模式」，工具用你的真实凭据依次实测
（账密登录 → 标准签到 → 按报错关键词判断 claim/sign/proof/ocr → JWT 登录），自动选中
检测到的模式并展示判定依据；全部探测不通时默认按标准签到，不影响手动改选。
exe 内置配置模板（settings/注释齐全，站点列表为空）：本地还没有 sites.yaml 时面板直接
按模板展示（只读，不写盘），首次通过表单加站或保存配置时才落盘到你的目录。
默认只监听 127.0.0.1 并自动弹浏览器；绑非本机地址时不设 token 会拒绝启动。

## 配置发现顺序

`CHECKIN_SITES` / `CHECKIN_CREDS` / `CHECKIN_STATE` / `CHECKIN_LEDGER` 环境变量
可覆盖四个文件路径（默认在脚本同目录）。Docker 镜像内状态默认写进 `/app/data` 卷。

## 挂了 Cloudflare Turnstile 的站

不少新站把 Turnstile 人机验证挂在**登录接口**上（签到接口本身通常不挂），账密自动化会被
`请完成人机验证` 拒掉。Turnstile token 必须由真实浏览器执行 JS 生成，纯 HTTP 无法伪造——
但**不需要**伪造：验证只在登录时做一次，把登录产物（凭证）复用即可完全绕开。

**做法（一次手动，长期自动）**：

1. 在浏览器正常登录该站一次
2. 提取凭证，二选一（**推荐 access_token**，不会过期）：
   - **Access Token**：站点「个人设置 → 系统访问令牌」生成（`sk-` 开头）
   - **Session Cookie**：F12 → Network → 任意 API 请求 → Request Headers → `Cookie:` 里
     `session=`（或 `new-api-session=`）的值
3. 面板「添加网站」把**登录方式**切到「直连凭证」贴进去；或手写配置：

```yaml
credentials:
  站名:
    access_token: "${TOKEN_X}"     # 或 session_cookie: "${CK_X}"，支持 ENV 占位
```

之后签到完全跳过 `/api/user/login`，用凭证直连 `/api/user/self` 验证 + 签到，不再触发
Turnstile。直连模式下**不会调登出接口**（防止把复用的 cookie 作废）。session cookie 过期后
（一般几天到几周，access_token 无此问题）重新提取一次即可。

同款思路参考了 Jasonliu-0/Newapi-checkin 等同类项目。付费打码平台（2captcha/CapSolver）
也可解 Turnstile token，但签到场景用凭证复用零成本、更稳。

> 注：仅用于你自己账号的自动化签到；不要拿去批量撞他人站点登录。

## 凭证解析规则

`sites.yaml` 只管"站是什么"，账号在 `creds.json`（或 sites.yaml 的 `credentials` 段）：

```json
{ "credentials": { "站名": { "username": "u", "password": "${PW_X}" } } }
```

站点条目可用 `credential: 别的键名` 让多站共用一份账号；`defaults.username` 兜底。
环境变量缺失时直接报错退出，不会拿空密码去撞登录接口。

## 安全提醒

- `creds.json` 含明文密码时请 `chmod 600` 并加入 `.gitignore`（本仓库默认忽略）
- Web 面板默认只监听 127.0.0.1；绑到局域网/公网必须设 `--ui-token`，否则任何人都能看你的
  配置和账本（面板读取 creds.json 原文，务必当作等同密码文件保护）
- 部分公益站 ToS 不明确，自动化频率已按"一天两次、登录即走"控制；站挂了或被风控请自行判断

## 已知边界

- 需要真浏览器过 SSO/Turnstile 的站点不支持（原个人版有 opencli 方案，依赖本机常驻浏览器，泛化后删除）
- `ocr` 模式体积大（ddddocr+onnxruntime 约 200MB），纯 API 站用户可无视
- 账本金额按 `quota_per_dollar: 500000` 换算，个别站定价不同，可在 settings 里改
