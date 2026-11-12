# cc-data-lab

CC_Codex 二期（26.11）**数据工程**主线基线：**采集任务控制台**（Flask 后端 + HTMX 前端页 + 站内 Mock 目录）。

## 领域

- **所属领域**：数据工程  
- **子方向**：数据采集、爬虫与 RPA  

## 技术栈

- Python 3.12+、Flask 3、SQLite  
- HTMX（CDN）+ Jinja 模板  
- pytest  

## 本地运行

```powershell
cd claude-workspace\cc-data-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

浏览器打开 http://127.0.0.1:5590 ，登录 **admin / 123456**。

流程：创建任务（填写每日可运行的起始/结束钟点）→ 选种子 `/mock/catalog/alpha` 或 `beta` → 详情页「运行采集」→ 查看落库（alpha 含重复标题，会演示去重跳过）。

## 每日钟点区间与补跑规则

- **创建任务**时必须填写起始钟点与结束钟点，均为 0–23 的整数，且结束必须晚于起始。区间为**半开区间 `[起始, 结束)`**：起始整点允许运行，结束整点即视为区间外。
- **是否允许写入正式结果只能由服务端决定**：所有时刻取自服务端时钟（`app/services/clock.py`），页面只显示服务端时刻，没有任何浏览器本地时钟逻辑；测试通过 `app.config["FIXED_NOW"]` 固定时刻。
- **区间外触发**：不写入 `crawl_records` 正式结果，本次见到的编码全部写入 `crawl_pending` 待补跑表，原因记为「不在区间」，并计入当天失败次数。
- **区间内触发（补跑）**：只处理 `crawl_pending` 中**尚未入库**的编码，成功后移入正式表并从待补跑表删除；**不会重新抓取整个目录冒充补跑**。同一编码靠 `UNIQUE(job_id, item_key)` 约束与入库前存在性检查双重保证不会重复入库。
- **当天上限**：同一任务当天因「不在区间」或「执行失败」累计到 **3 次**，当天不能再补跑（`job_daily_state` 按天计数）。想继续必须在详情页填写**非空解除原因**，解除仅对当天有效，次日计数重新从零开始。
- **并发守卫**：同一次运行状态还是 `running` 时，再次触发会被服务端直接拒绝（`RunRejected: job already running`）。

### 浏览器里怎样看出待补跑与已达上限

- **任务列表页**：每行显示「起始钟点 / 结束钟点 / 此刻在区间内（是|否，服务端判定）/ 待补跑条数（黄色 `N 条待补跑` 徽标）/ 当天失败次数 `n / 3`」，达到 3 次时出现红色 **「已达上限」** 徽标；页头展示服务端当前时刻。
- **任务详情页**：单独的「待补跑队列」表格逐条显示 item_key、标题、原因（如「不在区间」）和登记时间；队列清空后显示「暂无待补跑编码」。达上限后运行按钮替换为红色提示 **「当天失败/越界已达 3 次上限」** 和「解除原因」表单；空白原因提交会被拒绝并红字报错。「落库记录（正式结果）」表格只统计正式写入的数据，区间外触发不会在这里出现。

## Docker 一键启动（推荐用于快照前验收）

需已打开 **Docker Desktop**：

```powershell
cd claude-workspace\cc-data-lab
docker compose up -d --build
docker compose ps
```

- 页面：http://127.0.0.1:5590/login  
- Mock：http://127.0.0.1:5590/mock/catalog/alpha  
- 停止：`docker compose down`（保留库卷）· 清卷：`docker compose down -v`

`docker compose ps` 中 `STATUS` 含 **healthy** 表示服务可访问。线下表「环境可复现等级」可填 **已容器化，可一键起环境**。

## 前后端说明

| 层 | 说明 |
|----|------|
| 后端 | `app/` 路由、任务状态机、同步 runner、SQLite |
| 前端 | `templates/` + `static/`，浏览器可点操作 |
| Mock 源站 | `/mock/catalog/<id>` HTML，无需外网 |

## 初始快照

在首次 Claude 单轮跑题前 commit + push 本树（不含 `.venv/`、`instance/*.db`）。后续题目在同 repo 上叠加调度、RPA 步骤、增量水位等能力。

## 基线 vs 跑题目录

| 路径 | 角色 |
|------|------|
| `claude-workspace/cc-data-lab`（本目录） | **基线**：改功能、docker 验收 |
| `ccproject/cc-data-lab/data-XX` | **单题**：复制基线后挂容器、出快照 |

复制：`..\scripts\Copy-BaselineToQuestion.ps1 -QuestionId data-01`

## 跑题 Harness

见 `CCcodex/docs/ClaudeCode_Windows使用说明.md`（镜像 2.0）。容器挂载 **ccproject 下该题目录**，不是本基线路径。
