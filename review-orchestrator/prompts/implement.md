实施者负责分析、计划、修复、回归测试，并把机器可执行的验收写进 `checks.json`。审阅票只能由独立会话提交，实施笔记和预跑结果留在私有任务目录。

- `requirement.md`：只写原始需求和可观察的后果，不写诊断和结论。
- `commands.md`：中性的人工核对说明，例如 CI 范围、外网限制。
- `checks.json`：
  - `focused`：能区分缺陷的定向回归，基线上应失败，修复后应通过；
  - `suite`：相关套件，修复后必须全绿，需要外网的用例要排除并在 commands.md 说明；
  - `envs`：解释器和依赖安装命令，只装依赖，不要用 `pip install -e`；
  - `module`：被测包名，程序用它确认导入来自审阅副本；
  - `mutation`：变异测试的数量和超时。
- 用 `orchestrate.py start` 冻结和调度。退回后修改并重新 start（新哈希，从第 1 关重走）。需求或核对说明必须改时加 `--input-change=<理由>`，任何审阅意见都不能写进去。
- 普通文件任务用 `gate.py freeze-task`，然后 `orchestrate.py run`。
