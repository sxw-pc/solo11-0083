# 终端拾屏台 / termharvest

> **合规声明（置顶）**
>
> 本项目**仅可用于本人所有或已获书面授权的本地模拟/测试环境**。
> 它只做只读屏幕采集，不连接真实主机，不写入业务终端，不实现表单填报或远程控制。
> 仓库中的所有终端屏幕、业务主体、编号、名称和“凭据”均为合成 fixture，
> 不包含真实公司、真实域名、真实主机地址、真实账号或个人信息。

终端拾屏台把老式字符终端（固定 24×80 屏幕）的采集工作收敛为一条可离线复现的管线：

```
连接 loopback 终端 → 按会话脚本导航 → 抓取屏幕帧 → 按契约抽取字段
→ 声明式校验 → SQLite 幂等入库 → 留存哈希证据 → 生成自包含报告
```

## 项目定位

**是：**

- 面向固定格式字符终端的本地屏幕采集与结构化归档工具；
- 配置驱动：YAML 屏幕契约 + 会话脚本 + 字段/批次校验规则 + SQLite 台账；
- 提供 loopback-only 的确定性 mock 终端，可完全离线演示；
- 支持 live（连接 mock/授权本地终端）与 replay（JSONL 录制回放）双模式；
- 支持屏幕帧 SHA-256 留证、幂等入库、失败可解释、录制回放交叉核验。

**不是：**

- 网页爬虫、HTML 解析器或浏览器自动化工具；
- 真实 TN3270/TN5250/EBCDIC 实现，不连接真实大型机；
- 终端写操作 RPA、表单填报器或远程控制客户端；
- 调度平台、分布式任务系统、代理池或反爬工具；
- 通用报表平台或数据集成平台。

## 快速开始

要求 Python 3.10+、PyYAML 与 pytest。运行期只允许连接 `127.0.0.1` /
`localhost` / `::1`，不会发起任何外网请求。

```bash
# 1. 跑测试（无需固定端口、无需外部服务、无执行顺序依赖）
pytest -q

# 2. 一条命令跑完整演示：启动 mock、live 采集、replay 核验、清理进程
bash scripts/run_demo.sh

# 3. 单独启动 mock（端口 0 表示随机可用端口，启动后打印 MOCK_READY）
python -m termharvest.cli serve-mock \
  --scenario fixtures/mock_host/demo.json --port 0
```

演示输出在 `data/`：

- `data/termharvest.db`：SQLite 台账（run、frame、record、event）；
- `data/report/`：`report.json`、`report_summary.json`、`records.csv`、
  `report.md`、`report.html`；
- `data/report_replay/`：回放模式报告，用于和 live 记录交叉核验；
- `data/evidence/`：异常屏幕原始帧证据（仅在失败时产生）。

也可直接运行一次采集（需已有 loopback 终端）：

```bash
python -m termharvest.cli run \
  --session contracts/sessions/demo_session.yaml \
  --screens contracts/screens \
  --validators contracts/validators/demo_rules.yaml \
  --db data/termharvest.db \
  --out data/report \
  --mode live \
  --port 19771
```

从源码直接运行时仓库根目录提供了一个轻量 `termharvest/` 转发包，因此无需先安装。
也可以执行 `pip install -e .` 后使用 `termharvest` 命令。

## 目录结构

```text
contracts/
  screens/      屏幕契约：match、字段定位、列表行区、摘要字段
  sessions/     会话脚本：start、按键步骤、翻页、重试、target
  validators/   字段/记录/批次校验规则
fixtures/
  mock_host/    3 个确定性场景：demo、paged、error_screen
  cassettes/    JSONL 录制：live、replay、malformed
  expected/     期望记录与报告摘要
scripts/
  run_demo.sh   一键演示
  make_fixtures.py  重新生成 24×80 场景与 cassette
src/termharvest/  实现与 CLI
tests/            65 个离线测试
data/             运行期产物（已 gitignore）
```

## 管线

1. **配置加载**：严格校验 YAML，错误定位到文件/字段，错误码稳定。
2. **目标白名单**：建立 socket 前做无 DNS 的 loopback 校验；非 loopback 立即拒绝，
   写入 `events` 台账，错误码为 `E_TARGET_NOT_ALLOWED`。
3. **导航与抓帧**：发送按键令牌，接收 `\x1e` 结尾的屏幕帧；处理半包、粘包、
   64 KB 上限与超时；步骤带期望片段、重试、退避和证据留存。
4. **抽取**：按契约做绝对定位、标签定位、多行拼接、列表行区逐行抽取；
   同时保留原始值与规范化值；敏感字段默认跳过。
5. **翻页累计**：比较相邻页首行业务键、识别停止标记，重复记录不重复入库。
6. **校验**：字段类型/必填/正则/枚举/范围/日期格式、跨字段一致性、
   批次主键重复/重复记录/空结果/金额合计/条数一致。
7. **存储**：SQLite 台账；`record_key` 唯一，重复运行是幂等的；原始屏幕帧
   以 SHA-256 留存；事件带稳定上下文和 SHA-256。
8. **报告**：JSON/CSV/Markdown/HTML；HTML 内联 CSS、自包含、无外部资源。

### 抽取结果

每条记录包含：

```json
{
  "screen_id": "demo_customer",
  "frame_seq": 1,
  "raw_values": {"region_code": "０７"},
  "normalized_values": {"region_code": "07"},
  "frame_sha256": "...",
  "primary_key": {"customer_id": "C00042"},
  "record_key": "..."
}
```

规范化支持：去首尾空白、合并连续空格、全角数字转半角、转大写；空字符串转 `None`。
敏感字段（如 fixture 中的 `operator_token`）默认不抽取、不进入记录也不落库；
屏幕原始帧仍作为完整证据留存，其中的 token 是明显的合成占位文本。

## 脱敏拾屏包（可交付核验者）

`store_frame()` 与 `write_cassette()` 会把整屏原文写入本地台账与回放文件——
只跳过结构化字段并不能阻止屏上令牌进入共享证据。脱敏拾屏包是另一种产物：
原始帧只在本地内存中完成契约核验，导出的 JSONL 包可交给无权查看敏感字段的
核验者离线回放非敏感抽取结果，敏感值不会进入包、报告或错误上下文。

```bash
# 采集时导出（live 或 replay 均可）；--redact-scope 选 span（默认）或 row
python -m termharvest.cli run ... --redact-package out/pkg.jsonl --redact-scope span

# 核验者离线核验：帧摘要、契约摘要、投影指纹、帧序、trailer、非敏感结果重放
python -m termharvest.cli verify-redacted \
  --package out/pkg.jsonl --screens contracts/screens \
  --session contracts/sessions/demo_session.yaml \
  --validators contracts/validators/demo_rules.yaml
```

**定位与拒绝**：按屏幕契约的 `absolute`、`label/right`、`label/next_line`、
`multiline` 定位器把敏感值解析为 24×80 网格中的字符跨度，只遮挡值跨度，
保留标签与非敏感单元格。以下情况拒绝导出（错误含屏幕、字段、位置，不含原值）：
标签在多行或同行重复（`E_REDACT_LABEL_AMBIGUOUS`）、定位器越出网格
（`E_REDACT_LOCATOR_OUT_OF_BOUNDS`）、敏感跨度与必需非敏感字段或 match 片段重叠
（`E_REDACT_OVERLAP`）、敏感值在声明跨度之外仍出现（`E_REDACT_RESIDUAL_VALUE`）。
绝不猜测该遮挡哪一处。

**两种共享范围**：`span` 精确遮挡敏感值跨度（同行无关状态标记仍可见）；
`row` 遮挡敏感值所在整行（同行标记不公开）。两者对其余非敏感字段的回放结论一致
（`results_sha256` 相同）。若整行策略会遮挡必需非敏感字段，则拒绝整行导出，
不悄悄产生不完整报告。

**摘要模型**：逐帧记录 `source_frame_sha256`（原始帧摘要，来源声明，无法从脱敏包
重新验证）与 `redacted_sha256`（脱敏帧摘要，可自行验证）；包头绑定屏幕契约摘要
与脱敏规则版本，二者不与帧内容摘要混作同一种完整性证明。遮挡后的每一帧仍是合法
24×80 帧。

**回放核验**：校验脱敏帧摘要、契约摘要（契约漂移即拒绝）、规则版本、帧序与
trailer，重新运行非敏感字段抽取与批量校验，并与导出时记录的非敏感结果摘要比对；
敏感字段在回放结果中始终缺席。对带整屏指纹的旧契约，不拿脱敏帧去比原始整屏哈希，
改为独立校验非敏感可见单元格的投影指纹；无法解析敏感跨度的旧契约帧会被明确拒绝。

## 契约格式

屏幕契约示例（完整文件见 `contracts/screens/`）：

```yaml
screen_id: demo_customer
match: [CUSTOMER PROFILE, CUSTOMER ID]
primary_key: [customer_id]
fields:
  - name: customer_id
    locator:
      kind: label          # absolute | label | multiline
      label: CUSTOMER ID
      search_rows: [3, 3]  # 闭区间、0-based
      direction: right     # right | next_line
      length: 16
  - name: mailing_address
    locator:
      kind: multiline
      rows: [11, 12]
      col: 23
      length: 40
      join: " / "
```

列表行区：

```yaml
list_region:
  start_row: 5
  end_row: 19
  step: 1
  skip_blank: true
  columns:
    bill_id: {start: 1, end: 11}   # 半开区间、0-based
    amount: {start: 25, end: 36}
```

会话脚本定义 `start`/`steps`/`paging`/`retry`/`target`。按键令牌为大写字母数字，
例如 `ENTER`、`PF7`、`PF3`、`CLEAR`、`TAB`。客户端只发送按键，不发送任何业务写入。

## 协议

- 服务端连接后发送起始屏幕帧：24 行 × 80 列，UTF-8，以 `\x1e` 结束；
- 客户端发送一行按键令牌，以 `\n` 结束；
- 帧上限 64 KB；读取器保留跨 `recv` 缓冲，正确处理半包/粘包；
- mock 只绑定 loopback，拒绝 `0.0.0.0` 等地址；支持 `--once`。

场景 JSON 使用固定 24×80 行、按键转移表、`drop_keys`（超时行为）、
`delay_ms` 和 `unknown_key_screen`。

## Live 与 Replay

- **live**：按会话脚本连接 loopback mock/授权终端；每一步校验期望屏幕片段；
  失败保存原始帧并重试，重试耗尽返回 `E_STEP_RETRY_EXHAUSTED`（底层错误在 cause）。
- **replay**：读取 JSONL cassette，校验 header/trailer、session_id、步骤顺序、
  期望屏幕与帧完整性；不建立网络连接。malformed cassette 会返回
  `E_FRAME_MALFORMED`/`E_CASSETTE_PARSE`。
- 两种模式使用同一套抽取、校验、入库和报告代码。示例 fixture 的 live/replay
  记录完全一致，`scripts/run_demo.sh` 会自动交叉核验。

## 主要错误码

| 错误码 | 含义 |
| --- | --- |
| `E_CONFIG_PARSE` | YAML/JSON 无法解析 |
| `E_CONFIG_SCHEMA` | 契约字段、类型或结构非法 |
| `E_CONFIG_DUPLICATE_FIELD` | 重复字段/列 |
| `E_CONFIG_BAD_LOCATOR` | 定位器非法或越界 |
| `E_CONFIG_BAD_TARGET` | 配置中的 target 非 loopback |
| `E_TARGET_NOT_ALLOWED` | 建立连接前拒绝非 loopback 目标（同时写审计） |
| `E_FRAME_TIMEOUT` / `E_FRAME_TOO_LARGE` / `E_FRAME_MALFORMED` | 帧读取失败 |
| `E_SCREEN_UNEXPECTED` | 屏幕与步骤期望不一致，原始帧已留证 |
| `E_STEP_RETRY_EXHAUSTED` | 步骤重试耗尽 |
| `E_SCREEN_CONTRACT_MISMATCH` | 屏幕不满足屏幕契约 match |
| `E_SCREEN_FINGERPRINT_MISMATCH` | 指纹不一致 |
| `E_FIELD_*` / `E_RECORD_CONSISTENCY` / `E_BATCH_*` | 字段/记录/批次校验失败 |
| `E_CASSETTE_*` | cassette 解析、顺序或完整性失败 |
| `E_REDACT_LABEL_AMBIGUOUS` / `E_REDACT_LOCATOR_OUT_OF_BOUNDS` | 敏感标签重复或定位器越界，拒绝导出 |
| `E_REDACT_OVERLAP` / `E_REDACT_RESIDUAL_VALUE` | 遮挡会覆盖必需非敏感内容，或敏感值在声明跨度外残留 |
| `E_REDACT_CONTRACT_MISMATCH` | 脱敏包契约摘要或规则版本与本地不一致 |
| `E_REDACT_PACKAGE_*` | 脱敏包解析、摘要、帧序或 trailer 校验失败 |

配置错误优先于网络行为；只有 loopback 目标才可能进入连接阶段。

## 测试与确定性

```bash
pytest -q
```

测试使用随机端口、临时 SQLite 与固定时钟，覆盖：

- 契约解析与全部配置错误码；
- 帧协议半包/粘包/超长/非法编码/超时；
- 屏幕匹配、标签/绝对/多行定位、翻页去重、指纹；
- 字段/记录/批次校验与空结果；
- SQLite 幂等 upsert 与事件留证；
- mock loopback 绑定拒绝、导航、`--once`、超时行为；
- live/replay 端到端等价、非 loopback 审计；
- 异常屏幕证据、重试耗尽、malformed cassette；
- 报告字节级确定性、HTML 自包含与转义。

同一输入、契约与注入时钟下，记录顺序、错误顺序、报告字段与 sha256 均稳定。
不会使用当前系统时间替代注入时钟。

## 安全边界

- 唯一网络行为：连接 `127.0.0.1` / `localhost` / `::1`；
- 不解析网页、不访问外部 API、不依赖 CDN、不上传文件；
- 不提供写业务数据、修改终端状态或远程执行能力；
- 日志只记录元数据（step、screen、hash、大小），不打印屏幕正文或凭据；
- SQLite 与报告中的异常证据仅来自本地合成 fixture；
- 请只在本人所有或已获书面授权的本地环境使用本项目。
