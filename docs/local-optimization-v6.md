# 本地优化 v6（2026-11-21）

v6 在 v5 上修复了两项新复现的漏测，并使用基线类命名空间的实际读取键减少部分
过度选择。30 个公开样例三轮均零漏测，含选择器 CPU 的中位数从 130.441190 秒降至
128.908255 秒；模拟耗时从 128.523 秒降至 127.083 秒。

用户已授权准备线上实测，但本报告只记录本地证据。官方已知结果仍是提交 `5887d18`
的 468.45 秒模拟耗时、每轮一个漏测及 300 秒罚时。v6 尚无官方复核结果，也不能据此
断言已修复官方未披露的漏测。

## 公开样例测量

| 指标 | v5 | v6 |
| --- | ---: | ---: |
| 30 个公开样例，模拟耗时 | 128.523 秒 | 127.083 秒 |
| 三轮含 CPU 总分中位数 | 130.441190 秒 | 128.908255 秒 |
| 三轮漏测数 | 每轮 0 | 每轮 0 |

v6 三轮总分分别为 128.908255、128.913556、128.880101 秒。三轮模拟耗时均为
127.083 秒，选择器 CPU 分别为 1.825255、1.830556、1.797101 秒。
镜像为 `rwx-selector:v6-local`，本次测量的镜像 ID 为
`sha256:d0ea977481575e324bd4197198127b39be0fec4d7c3f04c880ebddbb60c7facf`。
完整测量见 `evaluation/local-v6-verified/report.json`，公开封存结果和哈希见 `docs/local-v6-results.json`。

本轮实际节省 1.440 秒模拟耗时、1.532935 秒含 CPU 中位数。未采用移除类字面量
定义行覆盖的探索方案；其讨论中的 8.126 秒模拟收益不是本版本已实现的结果。
样例 `535z` 由 170 个测试缩至 5 个，当前模拟开销为 9.793 秒，仍保留定义执行行的
消费者。尚无通用的类创建证明，不能只凭属性读取记录排除元类、类装饰器和原始
类命名空间生成的派生状态。

## 两项实际漏测及修复

| 定向改动 | v5 实际遗漏 | v6 行为与验证 |
| --- | --- | --- |
| 修改 `tests/fixtures/optimizer/tpc-h/tpc-h.sql` 首条查询为非法 SQL | 只选 2 个测试，漏掉 `TestExecutor::test_execute_tpch` | 补充类级 setup 依赖，选择 61 个测试；全部 1559 个省略测试直接执行通过 |
| 将 `tests/helpers.py` 的 `FIXTURES_DIR` 改为不存在的目录 | 只选 37 个测试，漏掉 `TestJsonpath::test_cts_file` | 模块声明可能经导入别名派生状态，保守选择全部 1620 个测试；无省略测试可执行 |

两项反例都有相同目标测试的原始版本通过、改动版本失败对照。仅补类级 setup
依赖仍无法修复导入别名反例：选择增加到 96 个时，目标测试仍被遗漏。
因此两项修复处理的是不同的依赖路径。

发布前复核还验证了同一别名机制的 EOF 变体：在 `helpers.py` 末尾追加赋值，
初始 v6 仍选 96 个测试并漏掉 JSONPath 目标。最终按新旧模块级声明 AST 比较，
覆盖 EOF 和空白间隙插入，改为全选 1620 个；原版通过/改版失败对照及旧选集已保存于
`evaluation/audit-next-round/module-alias-probes/validation-eof-fix/`。

对应证据为 `evaluation/audit-next-round/fixture-targets/report.json` 和
`evaluation/audit-next-round/module-alias-probes/validation-candidate-v6/report.json`。
这些是定向诊断反例，不是对官方隐藏补丁身份的判断。

## 类命名空间观察与保守边界

新增观察只对内建 `mappingproxy` 的直接 `class.__dict__.get(key)` 按键记录。
普通 `__dict__` 读取、迭代、别名以及不能证明语义的访问仍视为整个命名空间的消费。
自定义元类若覆盖 `__dict__` 或 `__getattribute__`，不能使用按键精度。
观察器保留真实命名空间对象，并保持接收者、方法查找和参数求值顺序。

选择器合并原有类字典依赖与新观察：只有新观察实际覆盖的旧运行上下文才允许按键
细化；未被新观察覆盖的旧读者继续保留。整体命名空间消费者、对应键消费者及
收集阶段的对应依赖仍保留。原有直接属性收集依赖不被新图清除。

类体中的裸名称读取也会生成未经过属性观察器的状态，例如
`DERIVED = transform(FIELD)`。新静态保护扫描新旧 AST，禁止对此类字段进行字面量
精确选择，包含函数默认值、装饰器和嵌套类头部等位置，并保守覆盖方法体。
实际基线中的 `Presto.JODA_TIME_MAPPING` 和 `SingleStore.VECTOR_TYPE_ALIASES`
因此保留类级选择。审计发现这些依赖并不等于已证明它们造成新增漏测；相应已执行的
目标改动测试仍属于直接字段读者。

`TSQL.WEEK_OFFSET` 没有该裸名称依赖；`TSQL.Tokenizer.QUOTES` 则在类创建时派生
后续消费的引号状态，继续受到直接属性收集保护。定义行覆盖仍保留，未对某个字段名
或公开补丁硬编码选择结果。

字面量精度还要求：除已允许的字段值外整文件 AST 严格相同；同行字段读者取并集；
排除特殊字段和调用表达式（包括 `literal_eval` 接受但可能被遮蔽的 `set()`）。

## 基线采集检查

旧图的类夹具包装发生在 pytest 已捕获绑定方法之后，未生成真实类级上下文。
本轮将包装提前到 `pytest_pycollect_makeitem`；独立补图在四个具有自定义类夹具的
测试类各执行一个基线哨兵测试，4 项通过、耗时 14.14 秒，合并 144 行和 35 个读取键的
类共享依赖。该补图不是完整测试套件执行；进程退出成功但保留了 DuckDB/nanobind
关闭时引用诊断。对固定基线的四类 setup/teardown/cleanup 已人工检查，原有完整图边保留。

命名空间图来自未修改的固定 SQLGlot 基线
`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`，完成了三个必需模式：

| 模式 | 实际结果 |
| --- | --- |
| warm | 1620 passed、1 skipped、25440 subtests passed，106.63 秒 |
| cold，逐测试清空分派缓存 | 1620 passed、1 skipped、25440 subtests passed，109.16 秒 |
| eager-import-collection | 主动导入后成功收集 1621 个测试，采集完成且无失败；不声称执行了整套测试 |

合并器要求三个模式齐全、完整标记为真、失败列表为空，并检查固定源码提交、源码
哈希、测试 ID 及生成器哈希一致。运行前记录生成器哈希，结束时检测采集代码是否变化。
图中的整体、opaque 和按字段读者及收集依赖取并集，保留每个输入和生成器的来源。

日志位于 `evaluation/namespace-profiles/final-warm/`、`final-cold/`、`final-eager/`；
合并来源见 `evaluation/candidate-v6-artifacts/namespace-provenance.json`。
失败标签和公开答案只用于离线评估，没有写入生产选择图。

## 定向 namespace 探针：直接省略测试验证

8 个探针使用种子 `20261123` 和公开记录的选择规则构造，覆盖多个 dialect 及
bool、int、string 字段，并另设一个 list 收集保护对照。`TSQL.WEEK_OFFSET` 改为
`-3`，不同于公开样例的 `-2`。生成时检查公开补丁重合，保留原源码、图和生成器哈希。
种子用于复现定向选择，不使这批探针成为无偏随机 benchmark 或独立隐藏留出集。

8 个探针均已完成，共直接执行并通过 12,478 个省略测试：

| 探针 | v6 选择 | 省略测试验证 |
| --- | ---: | --- |
| namespace-001，TSQL.WEEK_OFFSET | 5 | 1615 passed |
| namespace-002，Snowflake.COPY_PARAMS_ARE_CSV | 2 | 1618 passed |
| namespace-003，Spark2.INITCAP_DEFAULT_DELIMITER_CHARS | 3 | 1617 passed |
| namespace-004，ClickHouse.INDEX_OFFSET | 2 | 1618 passed |
| namespace-005，MySQL.Tokenizer.DASH_COMMENT_REQUIRES_BOUNDARY | 292 | 1328 passed |
| namespace-006，Exasol.NULL_ORDERING | 5 | 1615 passed |
| namespace-007，PRQL.DPIPE_IS_STRING_CONCAT | 3 | 1617 passed |
| namespace-008，TSQL.Tokenizer.QUOTES 收集保护对照 | 170 | 1450 passed |

证据位于 `evaluation/namespace-probes/manifest.json`、`prior-overlap-check.json` 和
`validation-candidate-v6/report.json`。另与全部历史补丁重建后的 AST 比较，8 项均无相同变更；仍属于定向诊断，不是无偏抽样。

## 142 个补丁的集合比较与历史证据继承

`evaluation/verified-v6-selection-comparison/report.json` 比较了 142 个已有补丁，
其中 8 个选择集合发生收窄：6 个 namespace 探针、公开样例 `535z` 和历史
`probe-022`。集合比较本身不是对省略测试的执行验证。

- 6 个收窄的 namespace 探针采用本轮直接执行省略测试的证据，完成状态见上表。
- 公开样例 `535z` 使用公开答案检查，三轮测量均未遗漏已知失败测试；不声称本轮
  重新执行了其所有省略测试。
- 历史 `probe-022` 在 `evaluation/probes/validation-v2-b/report.json` 中曾选择
  2 个测试、直接执行并通过其余 1618 个。最终选集包含当时两个测试，故当前 3 个
  已选测试对应的省略集合是历史通过集合的子集。28 份已完成验证记录均通过最终
  选集超集检查，见 `validated-supersets.json`。

对历史证据的继承要求补丁、固定基线和测试身份一致，并且新选择是已验证旧选择的
超集；这意味着当前省略集合是当时已通过集合的子集。继承不等于本轮重新执行，
也不能仅因当前集合比 v5 更小或测试数量相近就成立。

## 最终回归与线上状态

最终 Linux 单测 93 项全部通过，包括注解无赋值、同行多字段、类体派生读取、
模块 EOF/空白间隙新增声明和真实 pytest 生命周期回归。

本报告尚无 v6 官方提交结果。正式线上成绩、是否仍有隐藏漏测，以及资源环境差异，
均须等待线上执行后补充，不能从公开样例或定向探针外推。
