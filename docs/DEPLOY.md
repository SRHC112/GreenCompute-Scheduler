# 部署与推送说明（Git / GitHub）

本项目的「部署」指**把源码托管到 GitHub 仓库**，不包含在线服务部署（本项目为本地可运行的 CLI + 本地操作台，无需服务器）。

| 项 | 值 |
|---|---|
| 仓库 | <https://github.com/SRHC112/GreenCompute-Scheduler> |
| 主分支 | `main` |
| 许可 | Apache-2.0 |
| 本地目录 | `Claude-work/GreenCompute-Scheduler` |

---

## 一、当前部署状态

- 远端初始提交 `70e6611 Initial commit` 含 `LICENSE` 与 `README.md`，**已保留、未被覆盖**。
- 本地项目目录已执行 `git init -b main` 并关联 `origin`，基于远端提交**追加**新提交，全程不使用 `push --force`。
- 本次修复了两处环境问题：
  1. 删除全局失效代理 `http.https://github.com/.proxy = http://127.0.0.1:7890`；
  2. 修正 `.gitignore` 中 `runs/` → `runs/*`，使 `!runs/.gitkeep` 真正生效。

---

## 二、首次部署流程（可复现）

```bash
cd GreenCompute-Scheduler

git init -b main
git remote add origin https://github.com/SRHC112/GreenCompute-Scheduler

# 关键：远端已有 Initial commit，先继承历史，避免 unrelated histories / 强推
git fetch origin main
git reset --mixed origin/main                 # 只重置索引，工作区源码保持不变
git restore --source=origin/main --worktree -- LICENSE   # 取回远端的 LICENSE

# 编写 README.md / docs/DEPLOY.md / .gitignore 后
git add -A
git commit -m "feat: 绿算调度多智能体编排系统源码与说明文档"
git push -u origin main
```

> `git reset --mixed` 是**非破坏性**的：只改索引，不动工作区文件，适合「远端有初始提交、本地已有一堆源码」的场景。
> 切勿用 `git reset --hard` 或 `git push -f` 来同步初始仓库。

---

## 三、日常开发推送（dev）

```bash
git status                       # 先确认没有 .env / 大体积运行产物混入
git add -A
git commit -m "feat: xxx"
git push
```

建议：

- 功能改动开分支：`git checkout -b feat/xxx`，合并回 `main` 前先跑 `python -m unittest discover -s tests -v`。
- 提交信息前缀：`feat` / `fix` / `docs` / `test` / `refactor` / `chore`。
- 每次提交前执行 `git diff --cached --stat` 扫一眼，确认没有误提交密钥或 `runs/run_*` 产物。

## 四、发布与答辩前（prod）

```bash
python -m unittest discover -s tests -v        # 回归
python evaluation/run_eval.py                  # 确认最终评测数据（会覆盖已提交的结果文件）
git add -A && git commit -m "docs: 更新最终评测结果"
git tag -a v1.0.1 -m "参赛版本 v1.0.1"
git push && git push origin v1.0.1
```

- 答辩版建议打 tag，便于评委按固定版本复现。
- **不要**对 `main` 执行 `push --force`；如确需回滚，用 `git revert` 生成反向提交。

---

## 五、GitHub 认证配置（三选一）

本机系统级 Git 已配置 `credential.helper=manager`（Git Credential Manager），但没有缓存任何 GitHub 凭据，
首次推送需要完成一次登录。

| 方案 | 适用场景 | 做法 | 优缺点 |
|---|---|---|---|
| **GCM 浏览器登录**（推荐个人电脑） | 有图形界面的开发机 | 首次 `git push` 时按提示在浏览器完成 GitHub OAuth 授权，凭据由 Windows 凭据管理器保存 | 免手工管理 token；需要可交互桌面 |
| **Personal Access Token** | 无界面 / CI / 自动化 | GitHub → Settings → Developer settings → Personal access tokens → Fine-grained token，权限勾 `Contents: Read and Write`；推送时用户名填 `SRHC112`、密码填 token | 可脚本化；token 泄露风险高，用后建议设有效期 |
| **SSH Key** | 长期开发 | `ssh-keygen -t ed25519 -C "you@example.com"`，把 `~/.ssh/id_ed25519.pub` 加到 GitHub SSH Keys，再执行 `git remote set-url origin git@github.com:SRHC112/GreenCompute-Scheduler.git` | 一次配置长期可用；依赖 22 端口可达 |

查看当前认证配置：

```bash
git config --list --show-origin | grep -i credential
```

> ⚠️ **安全红线**：token / 私钥绝不写进源码、README、提交信息或 `.env` 并提交。
> 若误提交，第一时间在 GitHub 撤销该 token，并清理历史后再推送。

---

## 六、网络与代理排障（本机实测）

**症状**

```
fatal: unable to access 'https://github.com/...': Failed to connect to github.com:443
over proxy 127.0.0.1 ... Could not connect to server
```

**根因**：全局 Git 配置里残留一条专门针对 github.com 的代理，指向未启动的本地代理端口（常见于 Clash / V2Ray 类软件退出后）。

**排查与修复**

```bash
# 1) 查看所有代理相关配置与环境变量
git config --list --show-origin | grep -i proxy
env | grep -i proxy

# 2) 删除失效的 github 专用代理（本次部署已执行）
git config --global --unset-all "http.https://github.com/.proxy"

# 3) 验证连通性（能列出 refs 即正常）
git ls-remote https://github.com/SRHC112/GreenCompute-Scheduler
```

**如果确实需要代理**（先确认代理软件已启动、端口正确）：

```bash
# 永久（仅对 github.com 生效）
git config --global http."https://github.com/".proxy "http://127.0.0.1:7890"

# 或只对单次命令生效
git -c http.proxy=http://127.0.0.1:7890 push
```

**其他注意点**

- Git 会读取 `HTTPS_PROXY` / `HTTP_PROXY` 环境变量；排查时不要忽略它们。
- 临时绕过所有代理直连：`git -c http.proxy= -c https.proxy= ls-remote https://github.com/...`。
- 若使用公司/校园网，DNS 污染时可用 `git config --global http.sslBackend schannel`（本机已配置）。

---

## 七、入库内容边界（`.gitignore`）

| 类别 | 是否入库 | 说明 |
|---|---|---|
| `gcs/` `app/` `tests/` `knowledge/` `evaluation/*.py` | ✅ | 源码 |
| `evaluation/results.csv` `summary.md/json` `eval_report.html` | ✅ | 评测证据，随作品提交 |
| `docs/` `README.md` `requirements.txt` `LICENSE` | ✅ | 说明与依赖 |
| `runs/.gitkeep` | ✅ | 仅占位，保证克隆后 `runs/` 目录存在 |
| `runs/run_*` `runs/latest.html` | ❌ | 每次运行重新生成，体积会持续膨胀 |
| `__pycache__/` `*.pyc` `.venv/` | ❌ | Python 缓存与虚拟环境 |
| `.env` `.env.local` | ❌ | 模型密钥，绝不可入库 |
| `*.log` `evaluation/*.tmp` | ❌ | 临时文件 |

自检命令：

```bash
git status --short
git check-ignore -v runs/latest.html
git ls-files | grep -E "\.env|__pycache__|runs/run_" || echo "OK: 无敏感/无用文件入库"
```

---

## 八、常见坑清单

1. **远端已有初始提交**：直接 `git init && git push` 会因历史不相关被拒绝；正确做法是 `fetch` + 追加提交（见第二节）。
2. **强推覆盖**：`push -f` 会丢掉 `LICENSE` 与初始历史，评审协作场景下禁止使用。
3. **评测结果被覆盖**：`python evaluation/run_eval.py` 会重写 `results.csv` / `summary.*` / `eval_report.html`；改完先 `git diff` 再决定是否提交。
4. **Windows 换行符**：建议 `git config --global core.autocrlf true`，避免整文件 CR/LF diff。
5. **中文路径与编码**：提交信息用 UTF-8；Windows 终端可先 `chcp 65001`。
6. **大文件**：`runs/` 下 HTML/JSON 会随运行次数累积，务必保持忽略规则有效。
7. **密钥**：模型配置只走环境变量，代码不读取 `.env`；入库前用第七节命令自检。

---

## 九、部署验收清单

- [ ] `git log --oneline` 同时包含 `Initial commit` 与本次源码提交；
- [ ] `git status` 干净（除本地新产生的、已被忽略的运行产物）；
- [ ] `git ls-remote origin main` 返回的 commit 与本地 `git rev-parse HEAD` 一致；
- [ ] GitHub 网页端 README 正常渲染，`LICENSE` 显示为 Apache-2.0；
- [ ] 仓库中不存在 `.env`、`__pycache__`、`runs/run_*`；
- [ ] 另找一台机器 `git clone` 后，按 README 第二节安装、第三节运行，171 个单元测试全绿（其中 12 个为冒烟测试）。

---

## 十、持续集成（GitHub Actions）

工作流文件：`.github/workflows/ci.yml`（随代码一起版本化，改 CI 也走正常 PR 流程）。

| 项 | 配置 |
|---|---|
| 触发 | `push` / `pull_request` 到 `main`、`workflow_dispatch` 手动触发 |
| 权限 | `contents: read`（最小权限，不申请写权限） |
| 并发 | 同一 ref 只保留最新一次运行，自动取消旧运行 |
| `test` job | Ubuntu × Python 3.10/3.11/3.12/3.13，另加 Windows × Python 3.13 |
| `evaluation` job | Ubuntu × Python 3.13，跑六组对照实验并校验结果完整性 |

**检查项**：`compileall` 语法检查 → 单元测试（全量，171 个）→ `gcs.demo` 编排演示 → 看板/指标产物存在性 → 操作台 HTTP 探测（Linux）→ `run_eval.py` 六组实验与结果完整性（18 行 CSV、summary.json 结构与新鲜度、测试组 6 组 × 3 种子）。

**查看结果**：仓库首页 Actions 标签页，或 README 顶部的 CI 徽章。失败的 Artifact 仍会上传，便于下载 `console.html` / `results.csv` 定位问题。

**注意事项 / 常见坑**

1. CI 里 `python evaluation/run_eval.py` 会重新生成 `evaluation/` 下的结果文件，但**只在 runner 的工作区生效，不会提交回仓库**；本地重跑才会产生 `git diff`。
2. `results.csv` 以 `utf-8-sig`（带 BOM）写出，脚本里读取必须用 `encoding="utf-8-sig"`，否则首列字段名会变成 `\ufeffgroup`。
3. 工作流使用 `shell: python` 做跨平台产物校验，避免在 Windows runner 上写两套 bash/PowerShell 脚本。但 **Windows runner 的 stdout 默认 cp1252**，脚本里的中文 `print` 会抛 `UnicodeEncodeError`（2026-11-11 首次运行 Windows job 即因此失败，其余 5 个 job 全绿）。因此校验脚本统一用 ASCII 英文输出，并在开头 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` 兜底；新增校验步骤请沿用该约定。
6. 评测产物一致性：CI 校验 `summary.json` 必须与 `results.csv` 同批（时效检查），且含 6 组、操作台可直接消费的结构；否则无法通过。
4. 新增 Python 版本支持时，同步改矩阵与 README 里的环境要求；`setup-python` 的 pip 缓存依赖 `requirements.txt`，改依赖名要一并检查。
5. CI 不需要任何 Secret；项目可选的大模型配置（`GCS_LLM_*`）不参与 CI，系统按离线确定性模式运行，指标数值不受影响。

---

## 十一、一键安装脚本

| 文件 | 作用 |
|---|---|
| `install.py` | 唯一的安装逻辑（仅标准库，可单独下载运行） |
| `install.bat` / `install.sh` | 双击 / 直接执行的薄入口，只负责找到 Python 并转发参数 |

> `install.sh` 需要可执行位：从 zip 解压时已带 755；若走 git 克隆，仓库里的 index 记录是 100644，请执行一次 `git update-index --chmod=+x install.sh` 后再推送，否则 Linux/macOS 上 `./install.sh` 会 Permission denied（用 `bash install.sh` 调用则无需该权限）。
| `run.bat` / `run.sh` | **安装时生成**、已被 `.gitignore` 忽略；用 `.venv` 运行项目 |

**配置如何生效**：`ModelGateway` 只读 `os.getenv("GCS_LLM_*")`，项目不会自动加载 `.env`。
因此 `.env`（安装时可选生成）由 `python install.py --run ...` 读取后注入子进程的**环境变量**，
`run.bat` / `run.sh` 都走这个入口。全程不修改 `gcs/` 下任何源码，也不改变 README 第三节的手工运行命令。

**幂等与防覆盖**：`.venv`、`requirements.txt`、`runs/.gitkeep`、`.env`、`run.bat`、`run.sh`
一律「存在即跳过」；`.env` 已存在时即使选择接入也不改写，只提示手动编辑。
`--check` 是只读预检：不会 clone、不会安装依赖、**不改动项目内任何文件**（仅写入并立即删除一个可写性探针 `.gcs_install_probe`）；校验范围含 Python 版本、项目结构，以及唯一第三方运行时依赖 PyYAML（`>=6.0,<7.0`）——缺失/不可导入/版本越界时返回非零并给出可操作提示。

**检查顺序（v1.0.1 起）**：`--check` 先校验收**运行时依赖**，再做项目结构与写权限预检。
原因是全新机器上系统 Python 往往没有 PyYAML，这是最常见的情况，应当第一屏就给出
`pip install -r requirements.txt` 的可操作指引，而不是让它表现为结构类/配置类错误。
完整安装路径下，`precheck()` 的知识包语法预检在缺 PyYAML 时会**跳过并明确说明**（不再误报
「知识包解析失败」），知识包仍会在 venv 装好依赖后由单元测试加载校验——校验不丢失，只是
推迟到依赖就绪之后。因此全新机器可直接 `python install.py`（自建 venv 并装依赖），无需事先准备。

**CI 校验**：`test` job 增加 `python -m py_compile install.py` 与 `python install.py --check`，
覆盖全部 5 个平台 / 版本组合；安装脚本不需要任何 Secret。

**常见坑**

1. Windows 控制台默认代码页是 cp936（中文）/ cp1252（英文系统），安装脚本统一把 `sys.stdout` 与 `sys.stderr` 重配为 UTF-8 并带 `errors="replace"`，**不做 `chcp` 变更**（避免与子进程解码冲突）；子进程输出按本地首选编码 + `errors="replace"` 解码。新增输出时保持同一处理。
2. **`install.bat` 必须保持 CRLF + 纯 ASCII**：cmd.exe 按当前控制台代码页解析批处理，LF 换行叠加中文（多字节 DBCS）会把整行截断成碎片命令并被执行（典型报错 `'cho.' 不是内部或外部命令`）。仓库根 `.gitattributes` 已强制 `*.bat text eol=crlf`、`*.sh text eol=lf`、`*.py text eol=lf`——没有它，Windows 的 `core.autocrlf=true` 会把 `install.sh` 转成 CRLF，导致 Linux/macOS 上 shebang 变成 `#!/usr/bin/env bash\r` 而报 `bad interpreter`。所有中文提示统一由 `install.py` 以 UTF-8 输出，批处理只保留 ASCII 提示。
3. **`install.bat` 透传退出码**：脚本用 `set "GCS_RC=%errorlevel%"` 立即捕获，再 `exit /b %GCS_RC%`，因此 `install.bat --check` 的退出码与 `python install.py --check` 一致（自动化可据退出码判断成败）。
4. 只下载 `install.py` 时脚本会自动 clone；若目标目录已有内容，脚本拒绝覆盖并提示改用 `--dir`，绝不删除用户数据。
5. clone 出的旧版本仓库可能没有 `install.py`，脚本会自动把自身复制进项目目录，保证 `run.bat` 可用。
6. 安装校验阶段强制剥离 `GCS_LLM_*` 环境变量，避免误触发真实模型调用；配置只校验「能否被 ModelGateway 读到」，不发起网络请求、不消耗额度。
7. 生成的 `run.bat` / `run.sh` 不入仓，换机器后重新运行一次安装脚本即可再生成；生成时显式指定换行——`run.bat` 用 CRLF（`newline=""`，避免 `\r\r\n`），`run.sh` 用 LF（`newline="\n"`，避免 shebang 带 `\r`）。
8. Key 只写进被忽略的 `.env`（Unix 下权限 600）；选择「不接入」时连 `.env` 都不创建。

---

## 十二、操作台鉴权（GCS-CHG-2026-002）

**能力边界**：只作用于 `app/server.py` 的 HTTP 接口；CLI（`gcs.demo`/`gcs.replay`）、评测脚本、安装脚本、看板静态导出不受影响。不引入任何第三方依赖（纯标准库），与 SRS 3.5 零依赖约束兼容。

| 配置项 | 取值 | 说明 |
|---|---|---|
| `GCS_AUTH_MODE` | `off` / `loopback`（默认）/ `required` | `off`：完全关闭；`loopback`：本机回环来源免凭据，非回环需 Token；`required`：一律需 Token（公开只读 GET 仍放行） |
| `GCS_AUTH_TOKEN_HASH` | `salt$sha256hex` 格式 | 由 `python app/auth_tool.py generate` 生成；服务端只存哈希、不存明文，`hmac.compare_digest` 常量时间比对 |
| `GCS_AUTH_TOKEN_PREV_HASH` | 同上，可选 | 双凭据轮换：旧凭据派生值，配合 `GCS_AUTH_PREV_VALID_UNTIL`（截止时间，`rotate` 自动写入，默认 24 小时窗口）平滑过渡 |
| `GCS_AUTH_ISSUED_AT` | ISO 8601 日期 | 签发时间（由工具写入），用于 90 天过期判定：`loopback` 模式过期仅告警、`required` 模式过期拒绝启动（fail-closed） |
| `GCS_AUTH_MAX_AGE_DAYS` | 整数，默认 90 | 过期阈值天数 |
| `GCS_STRICT_AUTH` | `1` / `true` / `yes` / `on` | 对外部署硬约束：置位后**只接受 `required`**，`loopback`、`off` 一律拒绝启动（fail-closed），专治反代/容器下回环误判；不配则保持原有三态语义 |

**分级规则**：需鉴权接口 = `POST /api/run`（触发编排+写盘+可能消耗 LLM 预算）、`GET /api/run/<rid>`（运行详情）、`GET /console/<rid>`（看板）；公开接口 = `/`、`/api/policies`、`/api/eval`、`/api/runs`（CI 的 `curl -fsS /` 探测、`?autorun=1` 深链接、评委免凭据浏览评测看板均不受影响）。

`/api/runs` 只返回 `run_id`、`has_console`、`has_artifact`、`console_url` 四个字段，不含任何运行正文；支持 `?limit=N` 收窄返回条数（默认 50，允许 1–200，越界自动收敛），公开部署时可用它进一步压缩历史运行编号的枚举面。

**快速启用**（命令与 README 第四节一致）：

```bash
python app/auth_tool.py generate        # 首次生成 Token + 哈希（明文只显示一次；--force 覆盖）
python app/auth_tool.py rotate          # 轮换：旧哈希挪入 _PREV_HASH，--hours 可调窗口（默认 24）
python app/auth_tool.py status          # 查看模式 / 签发时间 / 过期状态（只读）
# 工具会直接把 GCS_AUTH_* 写入 .env（已被 .gitignore 排除），随后启动
python app/server.py
```

**运行时行为**：鉴权失败统一回 `401 {"error":"unauthorized"}`（不区分无凭据/错凭据/过期，防探测）；请求体超 1 MiB 回 `413`、`Content-Length` 为负值或不可解析回 `400`（两类错误分别报错，不再共用「请求体过大」文案）；启动横幅打印当前模式与凭据状态（不打印明文）；访问日志恢复为「时间/来源 IP/方法/路径/状态码」，**绝不记录 Authorization 头或请求正文**。

**安全注意**：
1. Token 明文只出现在生成时的一次性输出，勿存明文到任何文件；`.env` 已被 `.gitignore` 排除。
2. `loopback` 判定基于 `client_address`；若部署在反向代理/Docker 网桥后，代理会把来源改写为回环地址导致误放行——对外部署请直接用 `required`，并建议同时设置 `GCS_STRICT_AUTH=1`，让配置错误在启动阶段就失败而不是静默降级。
3. 静态 Token 方案（Bearer Header）的传输安全依赖 HTTPS；本仓库不提供 TLS，**HTTP 明文形态仅限本机回环或可信内网使用**，公网暴露需自行加反代 TLS。
4. 失败限流与多用户 RBAC 列入 V2.0（见变更单 001 延期清单）。

**内置安全响应头与 Host 校验**（无需配置，随服务生效）：

| 项 | 默认行为 | 说明 |
|---|---|---|
| `X-Content-Type-Options` | `nosniff` | 禁止 MIME 嗅探 |
| `X-Frame-Options` | `DENY` | 禁止被 iframe 嵌套（防点击劫持） |
| `Referrer-Policy` | `no-referrer` | 不外泄来源页面 |
| `Content-Security-Policy` | `default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'` | 页面为单文件内联 JS/CSS，故放开 inline；外部脚本/样式/连接一律阻断 |
| `Server` | `GreenCompute/1.0.1` | 不回解释器精确版本 |
| `Host` | 白名单校验；缺失 `400`、不在白名单 `403` | 白名单=回环名 + `--host` 绑定地址 + 本机主机名/FQDN/本机 IP；额外域名用 `GCS_ALLOWED_HOSTS=a,b` 追加 |

> 反向代理部署时：HSTS（`Strict-Transport-Security`）应在**代理层**下发（本服务不提供 TLS，自行下发无效）；
> 若代理会把 `Host` 改写为上游地址（如 `127.0.0.1:8000`），仍属白名单，无需额外配置；
> 若代理透传对外域名，请把该域名加入 `GCS_ALLOWED_HOSTS`，否则请求会被 `403`。
