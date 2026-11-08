# 编排安全进度（pipeline / company_run）

- `pansuan_pipeline.py --heartbeat-interval 15` 配置心跳间隔（秒），默认60；仅接受有限正数。此参数是诊断设置，不改变公司任务身份，续跑可调整。
- 进度仅写 stderr，每条为独立 JSON 并立即 flush；stdout 保留公司最终结果 JSON，以及失败时筛选后的安全恢复诊断，不混入进度。
- 进度结构：`type=pipeline_progress`，`event=stage|stage_completed|heartbeat|finished|failed`，`stage` 为固定安全阶段，`elapsed_seconds` 为本次调用耗时，`stage_elapsed_seconds` 为当前阶段耗时。不是百分比，也不是业务完成凭证。
- 精简交付（2026-11-07用户选定）：仅一次 `inventory_validation`，再 `delivery_review` → `delivery_coverage` → `delivery_engine` → `delivery_cache_transfer` → `publication`。缓存工具内的原数据/公式/缓存验收保留，不重复openpyxl扫描。跳过额外对账准备、金额对账及重复最终结构扫描；旧阶段名仍留白名单供历史兼容。缺少结构验收证据的续跑仍补 `final_validation`。引擎子进程超时600秒。`delivery.validation_profile=lean`，`audit.status=not_performed, verified=null`，不将未执行的金额对账报告为通过。
- 后台心跳覆盖同步函数与子进程等待；异常或返回非零记 failed，正常返回记 finished，并停止心跳线程。心跳只表示父编排仍存活，并不证明子进程在推进，不替代已有超时机制。已有 complete 续跑只核验，不会为显示完整阶段而重跑。
- 进度不含公司、日期、路径、命令参数、认证、响应或异常文本。子进程原始 stdout/stderr 不转发，防止凭据或网络响应泄漏；仅失败时按业务字段白名单转呈恢复协议。dry-run 也不再回显完整命令参数。
- ERP离线续跑：仅同公司同输入的显式续跑，调用真实核心`can_resume_offline`预检；已绑定待验收文件、已完成输出或不确定下载状态优先本地核验，跳过无法帮助恢复的ERP会话闸门。无效状态停止且安全报告，不回退登录或新建任务；实际ERP子进程仍在锁内重新验证全部绑定。首次普通导出仍执行会话闸门。
- ERP范围授权：仅显式 `--resume --erp-scope-approval-file <授权文件>` 透传实际ERP执行的 `--scope-approval-file`；不传给prepare/session，不进入company identity，不绕过ERP证据或公司日期校验。授权文件的内容和核验由ERP命令负责。

- 第二轮优化：`erp_scope_authorization` 为固定安全阶段。仅显式 `--erp-proven-partner-consent` 且本调用范围初筛符合时，在ERP子进程退出后调用既有离线生成器；不覆盖已有授权，完整六字段及合单证据核验成功才以原命令最多续跑一次。失败即停，不建新单/重下，最终结果仍须原ERP凭证核验。
- `erp_bridge_precheck` 为stdout独立诊断类型，只包含固定status与login_state=unverified，不是pipeline_progress也不是登录凭证。仅公司闸门通过后、账单导出前一次本机现有桥/ping；非本机或cookie-only跳过，不起桥、不取cookie、不查ERP。

最终结果JSON保留原字段，新增 `summary`：基于已有journal/receipt输出公司日期、主表及明细目录、17模块状态计数、处罚匹配与原因/缺单/待核查金额、重算/结构/金额对账结果；不额外读取工作簿。历史证据缺失为unknown/null，不推断成功；仅导出模式无主表，交付校验为not_applicable。

- 阶段切换前发出 `stage_completed`，其阶段耗时为已离开阶段的实测耗时；结束时同样报告最后阶段（失败时表示该阶段停止，不表示成功）。重复设置同阶段不重置计时。无需额外读取工作簿或日志中的业务内容进行性能定位。
- ERP闸门失败输出 `recovery_kind=bridge_connection|session_inspection`、`next_action=recover_own_erp_tab_then_resume`、`user_input_required=not_established`；连接缺失不等于未登录。Python不操作浏览器，由当前agent按技能接桥验证后原任务续跑，保持原有安全过滤。
- 最终 `summary` 增加 `execution_status/matching_status/conclusion/review_guidance`，顶层同时给 `conclusion`。流程完成与匹配完整独立；保留ERP原始兼容字段，不据其布尔值误判全流程失败。摘要仅来自机器证据，不另建手工状态源。

代理启动后台任务后保留job id并等待完成通知；只在确有诊断或等待结果需要时读取，不做短间隔重复轮询。无新事件的自动续轮不重复发送等待通知；只报告阶段变化、新异常、需要用户处理与最终结果。纯策略约束不替代宿主调度器，不能承诺宿主续轮停止或完成事件一定送达。

离线测试：`python -m unittest discover -s tests`（仅模拟子进程/网络、临时工作簿，不运行真实业务）。启动预检在dry-run后、创建journal和任何业务调用前执行一次，探测结束自动清理临时文件；检查通过不保证后续环境不变。ERP任务编号初筛接受正ASCII十进制字符串/整数，最终仍由原授权生成器严格核验，不改写证据。

`orchestration_policy.wait_decision` 的 `event_consumed` 与 `input_wait_decision` 的稳定等待键仅供agent保留会话上下文、去重读事件和催问，不写业务journal，不自动连接宿主调度器；自动续轮仍必须遵守主SKILL通知规则。
