# Codex Title Manager

修复 Codex 桌面端因标题模型不可用造成的自动命名失败，并通过原生接口管理用户审核过的历史标题清单。无常驻代理，不直接读写会话数据库。

> 社区兼容工具，与 OpenAI 无隶属关系。首版针对 Windows；依赖实验性 app-server 协议和当前 CLI 的内嵌目录格式，更新后可能需要调整。它不是所有“标题失败”问题的通用修复。

## 环境

- Windows、Python 3.10+、已安装且能正常聊天的 Codex 桌面端。
- `python -m pip install -r requirements.txt`。Python 3.11+ 无第三方运行依赖。
- 找到桌面正在使用的 `codex.exe`，通过 `--exe` 显式传入；不要直接使用不一致版本的另一个 CLI。
- 使用与桌面相同的 Codex home，默认 `$env:CODEX_HOME` 或用户目录下 `.codex`。

## 自动标题修复

先确认日志中的标题模型确实不可用、普通聊天模型可用。本方案提取当前 CLI 的完整模型目录，只移除指定不可用标题模型；依赖桌面已有回退逻辑，必须以桌面新聊天验收。

PowerShell（将第一行替换为自己的实际 CLI 路径）：

```powershell
$cli = (Get-Command codex.exe).Source  # 必须确认这是桌面实际使用的版本
python catalog_repair.py --exe $cli --remove-model gpt-6-luna --fallback-model gpt-6.1-sol prepare
python catalog_repair.py --exe $cli verify
python catalog_repair.py --exe $cli status
python catalog_repair.py --exe $cli apply
```

示例模型名仅表示已验证场景，使用者应按自身供应商修改。`verify` 检查模型目录列表、默认模型与能力字段，**不向模型发送生成请求，也不证明标题网络请求可用**。只有 verify 通过且安装文件/目录未变化才允许 apply。默认模型不是所选 fallback 时拒绝应用，不擅自改默认模型。

apply 使用原生 `config/batchWrite(expectedVersion)`，只添加 `model_catalog_json`，拒绝覆盖已有不同目录。写入前保存私有备份与 pending 状态。原生写入可能规范化 BOM/换行，不保证配置字节级一致。

随后完整退出并重新打开 Codex，**另建全新聊天**，等候自动标题并检查日志。此前命名失败的旧对话可能不会自动重试。已在一个实际环境确认 `thread_title` 改用可用模型、成功生成并写入；不是所有版本/供应商保证。

撤销：

```powershell
python catalog_repair.py --exe $cli restore
```

恢复只删除仍为本工具路径的设置，保留之后的其它配置，再重启桌面。应用升级后先 status；过期即拒绝 apply。重新 prepare 前应 restore；不要删除仍被配置引用的数据目录。

## 历史标题：用户审核清单

工具不替你读取全部聊天内容、自动推断或发送给模型。自行在仓库外创建私有 JSON，格式参考 `examples/rename-plan.example.json`。示例 ID 是虚构的，不能直接执行。

```powershell
$plan = Join-Path $env:TEMP 'my-title-plan.json'  # 请先自行创建并审核该文件
python history_titles.py --exe $cli preview $plan
python history_titles.py --exe $cli --exclude CURRENT_THREAD_ID --batch-size 3 --yes apply $plan
python history_titles.py status $plan
# 检查试点标题后，再执行下一批，每次至多 10 条
python history_titles.py --exe $cli --batch-size 10 --yes apply $plan
# 必要时恢复
python history_titles.py --exe $cli --yes restore $plan
```

preview 不改标题、不创建账本，但原生 app-server 启动自身可能产生内部日志。apply 默认只处理 3 条，每次最多 10 条；不会自动循环全部清单。跳过运行态、归档/未列出、未知元数据、标题基线冲突及空名称会话。请主动用 `--exclude` 排除当前执行会话；独立 app-server 的运行状态不能保证反映其它客户端的全部活动，操作前停止其它会话任务。

**空名称保守处理**：原生 name 为空而界面显示首条消息的旧会话会跳过；首版不开放本机专用 fallback 白名单。不直接写数据库，不假装可以还原内部 null。

每条写入前原子保存 pending，写后核对名称、ID、项目及时间；未知结果停止，重跑会先调和。账本按私有计划文件的绝对路径绑定，执行后不要修改或移动计划；否则恢复会拒绝或找不到原账本。保留计划与账本。restore 只恢复本次新名仍未变化且元数据一致的条目。

标题 API 没有 CAS，预读与写入之间仍有并发窗口；期间避免其它客户端改名或归档。恢复会跳过后改标题及元数据变化的条目。未知错误不要删除账本重试，应先检查私有状态。

## 私有数据与发布

运行数据默认位于 `$env:LOCALAPPDATA\codex-title-manager`（非 Windows 回退至用户数据目录）。`--data-dir` 可指定**独立的私有目录**，不可设为源码根目录或现有共享目录：工具会收紧其 Windows ACL。配置可通过 `--codex-home` 显式指定。

模型目录、安装路径、CLI 哈希、配置备份、原标题和会话 ID 只留在本机运行目录。备份可能包含密钥；不要上传运行目录、真实计划、Codex home 或整个旧工作目录。详见 [SECURITY.md](SECURITY.md)。本工具不提供上传、遥测或 GitHub API 功能；原生 app-server 本身仍遵从 Codex 的网络与日志行为。

## 开发、测试和打包

```powershell
python -m unittest discover -s tests -v
python tools/build_release.py --output-dir ../release
```

构建器只接受代码内列出的发布文件，扫描文本及 ZIP，输出源码 ZIP、SHA-256 与清单。新增文件必须显式加入白名单。可用 `--deny-file` 指定本机私有 JSON 字符串数组，阻止已知个人标识进入发布包（不得把该文件加入源码）。扫描覆盖常见秘密、个人路径和已知值，不能证明不存在所有未知隐私。

本仓库不含 Codex 二进制、原始模型目录、个人日志或真实聊天记录。MIT 许可仅覆盖本项目源码；Codex 产品和第三方服务保留各自条款。
