"""学习流程图：动态读取路由/工具定义，不读取.env、密钥、账本或用户问题。"""
import ast
import hashlib
from html import escape
from pathlib import Path

TRACKED=('task_admission.py','task_repository.py','task_execution.py','model_runtime.py','cancellation.py','errors.py','annual_data.py','annual_store.py','data.py','semantic_router.py','routing.py','task_parser.py','assistant_chat.py','capabilities.py','operating_guidance.py','query_plan.py','natural_dates.py','time_scope.py','business_calendar.py','sales_trend.py','group_sales.py','order_funnel.py','assessment.py','data_imports.py','tabular.py','web_service.py','web.py','workflows.py','tools.py','provider.py','local_model.py','context_memory.py','conversations.py','scope_patch.py','store_comparison.py','flow_view.py','runner.py')


# 评测入口独立于线上路由；修改评分或对比策略也触发学习图版本变化。
TRACKED+=('routing_benchmark.py','golden_evaluation.py','claw_eval_adapter.py')
TRACKED+=('narrative_contract.py',)
TRACKED+=('store_resolution.py','store_context.py','store_workspace.py')
TRACKED+=('store_ranking.py',)
TRACKED+=('query_context.py','clock.py')


def source_assign(path,name):
    """只解析常量赋值，绝不执行正在编辑的源码；语法错误由页面显示而非猜旧值。"""
    tree=ast.parse(path.read_text(encoding='utf-8'))
    for node in tree.body:
        if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id==name for t in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError('源码中未找到'+name)


class FlowView:
    """记录服务启动时源码指纹，与最新文件比较，提醒重启才能应用Python改动。"""
    def __init__(self,root):
        self.root=Path(root);self.code=self.root/'ecommerce_agent';self.started=self.digest()

    def digest(self):
        """哈希只包含固定白名单模块，不扫描凭据文件。"""
        return hashlib.sha256(b''.join((self.code/name).read_bytes() for name in TRACKED)).hexdigest()

    def snapshot(self,model_enabled=False):
        """返回图、输入输出、代码来源和当前能力清单，每次请求读取最新定义。"""
        routes=source_assign(self.code/'routing.py','ROUTES')
        tables=source_assign(self.code/'tools.py','TABLES')
        tools=[{'name':name,'description':'查询'+table+'表；SKU与日期由宿主固定'} for name,table in tables.items()]
        tree=ast.parse((self.code/'tools.py').read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='definition' and node.args and isinstance(node.args[0],ast.Constant):
                tools.append({'name':node.args[0].value,'description':ast.literal_eval(node.args[1])})
        stages=[
            ('默认范围与模型兜底','新问题缺门店→全部可访问门店；本店/具体店名→明确范围；追问→上一确认集合','规则确定时快捷执行；跨店共享业务计划未命中时最多一次模型规划，结构化参数统一校验后编译，不在规则与模型间循环；缺必要条件才追问，网络/校验失败保留错误；哪个店→门店销量排名，哪款→各店商品排名','store_resolution.py / store_context.py / store_comparison.py / store_ranking.py'),
            ('门店实体解析','服务端门店目录/别名/城市 + 原话 + 当前展示店 + 本轮查询集合 + 对话版本','最长实体匹配→提及/否定/纠正→查询/切店/比较/指代→复杂关系最多一次受限模型→范围复核；未知/歧义不查数，确认后保留原商品和日期，任务冻结目录版本与解析依据','store_resolution.py / store_context.py / conversations.py / local_model.py'),
            ('回答关系校验','模型草稿 + 当前证据 + 宿主日期/订单/分级契约','finish同时校验数字断言与有限关系：迟到取消归属、周间隔、宿主分级理由及因果/冲突边界；拒绝后在原预算内纠正，超限停止；通过不等于全文语义核验','narrative_contract.py / tools.py / runner.py'),
            ('简称消歧','目录候选 + 已识别日期 + 明确意图','多款简称只追问商品并保留日期；规划器可用__matched__保留候选集合，不能擅自扩大或缩小范围','task_parser.py / query_plan.py'),
            ('路由消融与模型工具链（独立评测入口）','固定年度数据、golden题目与评分器；rules_only / direct_model / hybrid','三路只改变理解路径，确定性执行保持一致；模型自主工具链独立评分；预算内真实API、失败留档、未知用量不补零，导出Claw轨迹与人工复核清单','routing_benchmark.py / golden_evaluation.py / claw_eval_adapter.py'),
            ('应用模块边界','HTTP/会话只调用应用入口；准入、仓库、执行器分别组合','task_admission生成任务卡与快照，不入队；task_repository管理SQLite事务与状态，不调模型；task_execution在提交后执行，事务回调关联业务扩展','web_service.py / task_admission.py / task_repository.py / task_execution.py'),
            ('模型资源管理（所有模型入口）','请求/后台任务独立账本 + 系统/当前问题/工具协议 + 宿主预算','每次调用前估算容量并预留预算；仅裁剪完整旧聊天轮次；工具证据不拆散，超限停止；供应商用量与容量估算分开，失败/取消用量未知不补零','model_runtime.py / provider.py / runner.py / conversations.py / web_service.py'),
            ('用户停止（贯穿各阶段）','请求ID或任务ID + 当前门店 + 取消令牌','规划/流式响应及时退出；队列取消持久化；每次模型/工具前检查；保留部分证据，迟到结果不覆盖取消终态；已发起API费用可能未知','cancellation.py / provider.py / web_service.py / conversations.py / web.py'),
            ('年度数据版本','固定种子 + 320款目录 + 365天 + 门店画像','逐日勾稽后发布不可变SQLite版本；答案仅供评测；任务只冻结范围与额外历史，不复制整年','annual_data.py / annual_store.py / data.py / store_workspace.py'),
            ('报表导入（独立入口）','CSV/XLSX后台报表 + 当前门店 + 字段/商品映射','整批预览确认后发布版本；错误不丢行；历史任务快照不变','tabular.py / data_imports.py'),
            ('输入','用户自然语言 + 对话ID/版本 + 服务端门店目录与查询上下文','仅需对话输入；原问题保持不变，范围从原话与上下文解析','web_service.py / conversations.py'),
            ('普通对话分流','系统自动识别问候、帮助、普通对话与经营查询','问候/帮助直接回复；其他普通对话按需模型SSE流式回复，无业务工具授权；保留已确认查询上下文','assistant_chat.py / conversations.py / provider.py / web.py'),
            ('对话状态','对话ID与版本 + 原话 + 各店独立查询上下文','先区分全店/本店等范围指代与具体店名；未知店名仍确认；局部条件合并保留未修改规格，基线修改不移动目标日','conversations.py / scope_patch.py'),
            ('统一查询上下文','已确认商品筛选、绝对日期窗口、排名维度与基线关系 + 本轮条件修改','QueryContext保存条件白名单；单店追问合并补丁，跨店按各自目录重新绑定；不再拼接规范问题逐店解析，不继承证据或权限；旧会话仅兼容解析商品筛选短语','query_context.py / task_parser.py / task_admission.py / store_comparison.py'),
            ('跨店查询与两类排名','默认全部或明确指定的门店集合 + 共享查询计划，最多8店','计划只编译一次，各店重新准入并取证；哪个店最好→按各店范围内销量排门店；各店哪款最好→各店独立商品排名；并列保留，缺失不补零','store_comparison.py / store_ranking.py / group_sales.py'),
            ('规则快捷路径','原话 + 只读边界 + 已识别实体与日期','完整明确的查询直接编译；无法确定时进入候选检索与统一模型规划，不能把规则未命中当作用户缺信息','routing.py / task_parser.py / query_plan.py'),
            ('本地向量候选检索','规则未完整解析的问题 + 人工维护意图示例 + 本地BGE索引','余弦相似度按意图汇总；相似例句仅供规划参考；不复用例句的日期或商品；故障继续模型规划','semantic_router.py / data/intent_examples.json'),
            ('实体与日期校验','路由结果 + 商品目录 + 口语/节日日期','中文数字与天/周统一规范化；单款或类别范围；节日当天按本年，节日期间需明确范围','task_parser.py / natural_dates.py / time_scope.py'),
            ('统一查询计划入口','原问题 + 向量候选 + 目录白名单 + 已解析日期；最多一次模型规划','模型可拒绝相似例句；统一确定意图、范围、日期、操作；计划重新经过业务准入','task_parser.py / query_plan.py / local_model.py'),
            ('统一问题与恢复动作','解析状态 / 模型HTTP错误 / 计划校验 / 执行失败','真缺条件才追问；缺数据/能力外分别说明；超时与鉴权失败不追问；保留查询上下文，重试须用户重新发送','errors.py / provider.py / conversations.py / web_assets/app.js'),
            ('统一能力契约','步骤依赖 + 意图/范围/指标 + 冻结门店与日期','取数前再次验证能力、参数、DAG和工具白名单；未接入的数据域明确拒绝，不执行其他分支代答','capabilities.py / workflows.py'),
            ('背景检索与任务队列','准入任务 + 已确认且适用的门店背景 + 待裁决遮蔽','最多3条背景与快照同事务提交；商品级覆盖门店级时回执与正文显式披露，不再静默；提交后通知执行器；重启按仓库状态恢复队列，幂等复用先于模型调用','context_memory.py / task_admission.py / task_repository.py / task_execution.py / web_service.py'),
            ('背景遮蔽裁决','同一事项的门店级与商品级同时有效且时间重叠','只登记覆盖关系，不判断内容真假；保留一方时撤回另一方而非删除，历史快照不变；一方失效自动关闭；裁决写入两侧事件','context_memory.py / web.py / web_assets/app.js'),
            ('执行','专项查询或调查模型循环；销量调查按可用来源/明确诉求追加支付日批次工具','多基线、日历与订单取消/完成/待完结；跨日取消归原支付日，保留截止时间','workflows.py / runner.py / tools.py / order_funnel.py'),
            ('综合判断','绝对损失 + 商品生命周期 + 同星期历史 + 假期/活动可比性','正常波动 / 值得关注 / 证据不足 / 数据冲突；保留历史筛选与策略版本','assessment.py'),
            ('输出','计算结果与实际观察证据','发生什么 → 证据支持什么 → 还缺什么 → 建议先查什么；冲突优先核对，建议不代表已执行动作','operating_guidance.py / runner.py / web_assets/app.js'),
        ]
        # 表格按实际主链路排列；旁路能力放在后面，避免把评测误当每次请求必经步骤。
        order=['输入','对话状态','统一查询上下文','默认范围与模型兜底','门店实体解析','普通对话分流',
               '规则快捷路径','本地向量候选检索','统一查询计划入口','实体与日期校验','简称消歧',
               '统一能力契约','跨店查询与两类排名','背景检索与任务队列','背景遮蔽裁决','执行','综合判断','回答关系校验','输出',
               '统一问题与恢复动作','应用模块边界','模型资源管理（所有模型入口）','用户停止（贯穿各阶段）',
               '年度数据版本','报表导入（独立入口）','路由消融与模型工具链（独立评测入口）']
        stages.sort(key=lambda stage:order.index(stage[0]))
        digest=self.digest()
        return {'version':digest[:12],'restart_required':digest!=self.started,'model_enabled':bool(model_enabled),
            'routes':[{'name':k,**v} for k,v in routes.items()],'tools':tools,
            'capabilities':{'version':source_assign(self.code/'capabilities.py','CONTRACT_VERSION'),
                'limits':source_assign(self.code/'capabilities.py','LIMITS'),'operations':source_assign(self.code/'capabilities.py','CAPABILITIES')},
            'stages':[dict(stage=n,input=i,output=o,source=s) for n,i,o,s in stages],
            'svg':render_svg(),
            'maintenance':'路由与工具清单直接读取源码，每5秒检查一次。流程拓扑在flow_view.py维护；调整执行顺序时须同步更新此文件。Python改动需重启服务才会执行。'}


def render_svg():
    """输出主链路SVG；稳定节点/边标识用于检查拓扑，模型规划不与工具执行混为一层。"""
    items=['<svg xmlns="http://www.w3.org/2000/svg" width="1480" height="2040" viewBox="0 0 1480 2040" role="img" aria-label="花迹当前主链路：门店范围、查询规划、统一校验、工具执行与结果">',
           '<title>花迹 · 从经营问题到可追溯的回答</title>',
           '<defs><marker id="flow-arrow" markerWidth="9" markerHeight="7" refX="8" refY="3.5" orient="auto"><path d="M0 0L9 3.5L0 7Z" fill="#8e7545"/></marker></defs>',
           '<rect width="1480" height="2040" fill="#fffdf8"/>']

    def text(x,y,value,size=17,color='#766951',weight='400'):
        items.append(f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" fill="{color}" font-family="Microsoft YaHei,sans-serif">{escape(value)}</text>')

    def box(key,x,y,w,title,lines,color='#f5f0e3',height=118):
        items.append(f'<g data-node-id="{key}"><rect x="{x}" y="{y}" width="{w}" height="{height}" rx="12" fill="{color}" stroke="#d5c9af"/>')
        text(x+20,y+31,title,21,'#443a29','600')
        for i,line in enumerate(lines):text(x+20,y+60+i*25,line)
        items.append('</g>')

    def edge(source,target,path,label='',x=0,y=0):
        items.append(f'<path data-from="{source}" data-to="{target}" d="{path}" fill="none" stroke="#8e7545" stroke-width="2" marker-end="url(#flow-arrow)"/>')
        if label:text(x,y,label,16,'#6e5d3d')

    text(40,42,'花迹 · 当前主链路',29,'#443a29','600')
    text(40,73,'实线表示请求流转；右侧为分流与恢复；左侧为支撑能力。模型不直接获得任意查数权限。')
    # 先画连接，再画节点，确保边线不会覆盖卡片文字。
    edge('input','stores','M730 228V270')
    edge('stores','rules','M730 438V490')
    edge('stores','chat','M970 300H1030')
    edge('stores','clarify','M970 410H1000V580H1030','真实范围歧义',1010,458)
    edge('rules','retrieval','M970 535H985V749H1030','未完整解析',1000,681)
    edge('retrieval','planner','M1235 808V850','候选 / 检索故障',1250,834)
    edge('rules','validate','M730 608V1010','完整明确 → 规则快捷路径',750,964)
    edge('planner','validate','M1235 968V1070H970','最多一次共享业务计划',1010,1046)
    edge('validate','clarify','M970 1100H1000V650H1030')
    edge('validate','errors','M970 1145H1235V1190','能力外 / 服务或校验失败',1010,1172)
    edge('validate','queue','M730 1178V1230','通过：逐店重新准入',750,1213)
    edge('queue','lookup','M730 1373V1410H270V1450','汇总 / 排名 / 专项查询',330,1432)
    edge('queue','investigate','M730 1410H1165V1450','销量变化调查',1000,1432)
    edge('lookup','facts','M270 1618V1700H490')
    edge('investigate','facts','M1165 1618V1700H970')
    edge('facts','output','M730 1778V1830')
    box('input',490,110,480,'01  一句自然语言',['原问题 + 对话ID / 版本','从服务端目录与上下文解析范围','不要求手动选择事项或商品规格'])
    box('stores',490,270,480,'02  门店范围与对话上下文',['新查询未提门店 → 全部可访问门店','本店 / 具体店名 → 指定范围','追问 → QueryContext合并已确认条件','复杂门店关系：最多一次受限模型解析','纯切店只恢复上下文，不查询经营数据'],height=168)
    box('chat',1030,270,410,'普通对话 · 自动分流',['问候 / 帮助直接回复','其他按需模型SSE流式回复','无经营工具授权，保留查询上下文'],'#eef3fb')
    box('rules',490,490,480,'03  规则只承担快捷路径',['边界检查 + 日期 / 商品 / 操作识别','口语日期与品类范围由宿主解析','无法确定 → 候选检索 + 模型规划'])
    box('clarify',1030,520,410,'必要信息缺失才追问',['未知店名 / 共享别名 / 真正歧义','保留已确定日期、商品与门店','用户补充后，携带上下文重新准入','未命中规则本身不是追问理由'],'#fff4df',143)
    box('retrieval',1030,690,410,'本地BGE · 向量候选检索',['相似例句与意图得分仅供参考','不复制示例中的日期或商品','索引故障仍进入模型规划'],'#edf5ef')
    box('planner',1030,850,410,'按需 · 一次业务查询规划',['原话 + 候选 + 目录 / 日期约束','生成结构化查询计划，不生成SQL','不再回规则与模型反复改写'],'#fff4df')
    box('validate',490,1010,480,'04  统一校验与查询口径',['核验门店、日期、商品、操作、指标','校验白名单 / 依赖；模型不能扩大权限','结构化条件逐店重绑定，不重写原话','缺数据、能力外与模型故障分别处理'],height=168)
    box('errors',1030,1190,410,'明确失败类型与恢复动作',['缺数据不当作零；网络错不当作缺条件','保留查询上下文与错误记录','停止执行；重试由用户重新发送'],'#fff0eb')
    box('queue',490,1230,480,'05  任务快照 → 仓库 → 队列',['冻结范围、数据版本与适用门店背景','同事务提交后入队，幂等与状态持久化','每店独立取证；取消不被迟到结果覆盖','最多8店；重启恢复队列，运行中标中断'],height=143)
    box('lookup',40,1450,460,'06A  确定性查询 · 不调模型',['ToolRegistry白名单查询与程序计算','哪个店最好 → 店内范围销量排门店','各店哪款最好 → 各店分别排商品','品类 / 单款 / 趋势 / 库存 / 活动','并列保留；未上架与缺失分开处理'],'#eef3fb',168)
    box('investigate',890,1450,550,'06B  调查 · 模型与工具循环',['DeepSeek → 工具请求 → 注册表校验执行','工具证据回传 → analyze_metrics → finish','关闭模型时使用确定性调查工作流','批次 / 请求 / 时间 / token预算与取消控制','数字断言 + 有限关系校验；拒绝后预算内纠正'],'#edf5ef',168)
    box('facts',490,1660,480,'07  事实、证据与综合判断',['绝对损失 / 生命周期 / 同星期历史','日历、活动可比性、订单取消批次','正常波动 / 值得关注 / 不足 / 冲突'])
    box('output',490,1830,480,'08  回答与可视化',['发生什么 → 证据 → 缺口 → 下一步','单店或跨店汇总、排名与变化曲线','保留任务、工具轨迹、证据与用量'])
    # 支撑能力单独列出，不画成每次用户查询的必经步骤。
    box('data',40,270,390,'数据底座 · 独立维护',['年度合成数据：320款 / 365天','CSV / Excel导入与版本发布','既有任务仍使用冻结数据快照'],'#f1f3ed')
    box('memory',40,490,390,'门店记忆 · 背景而非事实',['只取已确认、适用的门店背景','最多3条 / 1000字，随任务留档','商品级覆盖门店级时显式披露','裁决：保留一方或确认并存','不能替代当日证据或工具权限'],'#f1f3ed',180)
    box('budget',40,710,390,'贯穿全程 · 预算与取消',['门店解析、业务规划分别记账','调查工具循环可调用多轮模型','容量、超时、预算、取消统一管理'],'#f1f3ed')
    box('evaluation',40,930,390,'评测 · 独立入口',['Golden / badcase / Claw轨迹','规则、直接模型、混合路径对照','不在每次用户请求中运行评测'],'#f1f3ed')
    text(40,2010,'阅读提示：门店解析模型、业务查询规划和调查工具循环是三个不同入口；并非整次任务最多调用一次模型。',16)
    items.append('</svg>')
    return ''.join(items)


