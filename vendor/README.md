# 外部评测快照（Vendor）

这里存放可选的第三方评测代码，仅评测入口使用；核心运行时（`ecommerce_agent/`）仍保持
零第三方依赖。快照不参与业务查询，也不会被服务进程自动加载。

## Claw-Eval

- 上游：<https://github.com/claw-eval/claw-eval>
- 固定 commit：`5680b8b11ff2ee5dd2b07b89086a29a5c5c984d7`
- 获取方式：官方源码快照（非 git clone），逐文件 blob SHA 记录在
  [`claw-eval/SOURCE_SNAPSHOT.json`](claw-eval/SOURCE_SNAPSHOT.json)
- 许可证：MIT，见 [`claw-eval/LICENSE`](claw-eval/LICENSE)
- 用途：`ecommerce_agent/claw_eval_adapter.py` 把本地评测轨迹导出为上游
  Task/Trace 格式，再用上游 reader/grader 重新评分。

适配层支持：

```bash
# 录制：从本地 quality-report.json 生成轨迹、汇总与 manifest
python -m ecommerce_agent.claw_eval_adapter --report runs/.../quality-report.json --output runs/claw-record

# 回放：只读录制目录重新评分，不读原始报告、不调用模型、不联网
python -m ecommerce_agent.claw_eval_adapter --replay runs/claw-record --output runs/claw-replay
```

`tests/fixtures/claw-replay/` 是冻结回放样例；CI 会直接回放它。回放验证的是
“固定快照 + 导出的轨迹 + 上游评分器”仍能复现录制分数，不代表官方榜单成绩。

## 依赖与升级

- 仅评测层需要 `pydantic>=2` 与 `pyyaml>=6`；CI 单独安装，核心服务不依赖。
- 升级快照时：拉取新的上游 commit，替换整个 `vendor/claw-eval`，更新
  `SOURCE_SNAPSHOT.json` 与本文件的 commit，重新生成回放样例，并跑
  `python -m unittest tests.test_quality_lab tests.test_routing_benchmark`。
- 不修改 vendor 源码来让测试通过；需要适配时改 `claw_eval_adapter.py`。