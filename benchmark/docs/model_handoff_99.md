# Benchmark 模型交接：当前四方法与五轮部署

更新日期：2026-10-17（Asia/Shanghai）。保留 `model_handoff_99.md` 文件名以兼容已有链接；**99 题已不是当前部署目标**。
本文供接手 GPT 按当前代码完成配置与部署。旧全文保存在原工作区被忽略的 `runs/publication-prep-20261017-v1/model_handoff_99.before.md`，不随发布上传。

## 1. 当前范围与事实来源

发布支持 AMI、Mem0、InvMem、ReFind。LightMem / MemMA / LLMLingua 权重、环境和实验不进入本次发布。
本地原仓库保留历史代码；发布工具生成精简副本，不删除实验或重写现有队列清单。

当前数据为 `family40-history30-five-seeds-v3`：完整文本母库 10,649 题，BEAM 限 100K 后候选 9,249 题，取消原生题型预选。
先按 seed 抽原生完整历史，再补充历史以满足可用小类至少 2 题、每轮至少 91 题；历史最多 30 个、大类最多 40 题。轮内不重复，轮间允许重合，来源选择不使用 gold 或方法得分。

| seed | QA | 选中历史 | 最终有题历史 |
|---|---:|---:|---:|
| 17 | 202 | 23 | 22 |
| 18 | 199 | 23 | 23 |
| 19 | 196 | 23 | 22 |
| 20 | 201 | 24 | 24 |
| 21 | 205 | 24 | 21 |

合计 1,003 题次、791 道不同题目、每方法 112 个历史任务。四方法共用相同输入；每方法、每轮、每历史独立 scope，不跨轮复用记忆。
来源 hash、题目 ID 和预期数量已转成可移植的 `configs/reproduction/` 文件。旧 99 题分卷不能被当作当前五轮交付清单。

接手模型先读 `README.md`、`docs/evaluation_contract.md`、`configs/README.md`，再读 `configs/reproduction/`。
执行重点为 `pipeline/{prepare,reader,evaluate,aggregate}.py`、`runtime/{queue,worker,model_client}.py` 及所选 adapter。
InvMem/ReFind 另读 `integrations/native_bridge/{runner,worker,common}.py`。
固定版本与补丁以 `sources/publication-baselines.lock.json` 为准；导出包中的 `sources/baselines.lock.json` 也只列这四种方法。

## 2. 代码与评分边界

公共链路：prepare → 原生 add/finalize/search → 共同 Reader/Answer → 原生评测 → aggregate/reliability。
记忆 worker 只读 History 和 Query；gold、rubric、正确选项和能力标签留在评测侧。一个历史的所有回答保存后，队列才打开评分目标。

AMI/Mem0 走公共队列。InvMem/ReFind 保留已验证的方法侧桥接：先生成同格式检索断点，再调用公共 `queue.execute_task`、`publish` 和 scorer。
两者尚未注册进公共方法工厂；**不要把四方法 JSON 直接传给公共队列**。配置生成器同时生成逐方法 JSON，应使用下文对应入口。桥接冻结自身与 adapter 并检查 hash，不从历史 `runs/` 导入执行代码。

LoCoMo/LME 使用 judged accuracy；CLBench strict accuracy；PersonaMem v1 MCQ accuracy，v2 生成式 preference adherence；BEAM rubric mean。
CLBench 当前使用 `clbench-indexed-status-v1`，保留原生判分要求，用编号对象返回逐 rubric 状态后还原为列表；检查数量、重复键、合法状态和总分一致性。无完整有效响应则保留 unscored，不能补零或手工修分。
BEAM event-ordering 的短 yes/no 附加诊断保留原规则，不替代 rubric 主分。

每轮 Overall 为六数据集主分的等权平均，字段 `local_overall_macro_benchmark`；五轮报告均值、样本方差（分母 4）和样本标准差。
能力映射是本地原生题型代理，非 AML 官方逐题标签；没有题目的能力为 N/A。

## 3. 当前默认值与历史实验条件

实际 prepared/运行 `manifest.json` 的冻结 runtime 才是依据。修改配置文件不会改变已冻结运行。

| 字段 | 当前 `configs/runtime.json` | 四方法部署 `configs/reproduction/runtime.json` |
|---|---|---|
| `top_k` | null：公共层不额外限数量 | 100 |
| `reader_token_budget` | null：公共层不丢返回证据 | 4096，整块放不下则跳过 |
| `answer_input_token_limit` | null | 119000 |
| `answer_max_tokens` | null，网关解析为代码中模型上限 16384 | 相同 |
| `judge_max_tokens` | 16384 | 16384 |
| `memory_default_max_tokens` | 2048 | 2048 |
| 请求 / worker 超时 | 45 / 1800 秒 | 180 / 86400 秒 |

共用 `gpt-4o-mini`、`o200k_base`、Answer/Judge temperature=0；代码检查 provider 返回模型 allowlist。
共享 embedding 为 `text-embedding-v4`、1024 维、每批最多 10，使用 UTF-8 2048 字节分块及归一化加权合并。
ReFind 原生 BM25/agent 不调用 embedding；其 planner 保留 900 token、最多 4 次动作、内部 top5、邻接窗口 2、session RRF。

**InvMem/ReFind 的固定原生 HTTP schema 要求整数 top_k**；桥接明确拒绝 null，不悄悄改成 100。
四方法配置生成器显式采用已验证的有界配置。若要求四方法都运行无额外截断的新协议，需要另行适配原生接口、记录变化并重新验证；现有实验尚未覆盖该条件。

历史 Mem0 五轮 Answer 为 8192；AMI/InvMem/ReFind 后续 Answer 为模型上限，Judge 也存在补评及预算变化。
新统一配置不能用于声称逐请求重放历史成绩。ReFind 曾因返回证据块均超过 4096 而得到空 Reader context；提高输出预算不解决输入预算问题。比较时应列明实际运行条件。

## 4. 源码、凭据和公共环境

建议放到目标 `ami-reproduction` fork 的 `benchmark/` 下，保留根 AMI 源码、README、LICENSE、ATTRIBUTION 和 upstream 记录。
以下命令从 Benchmark 根目录执行，不依赖用户名、Conda 路径或旧服务器目录。

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[api,data]'
.venv/bin/python -m pip check
cp .env.example .env
chmod 600 .env
```

编辑 `.env` 填写聊天和 embedding endpoint/key，不打印正文、不写进 JSON 或提交。
原工作区 `.env` 已被 Git 跟踪且存在于旧历史，`.gitignore` 不能撤销跟踪。迁入 fork 使用发布导出目录，不能推送、合并或搬运旧 `.git`。

## 5. 原生方法安装

只用 lock 固定提交；原生源码不包含在发布包中，分别获取并创建环境。已有目录先检查版本及本地差异，避免覆盖。

```bash
mkdir -p baselines
git clone https://github.com/mem0ai/mem0.git baselines/mem0
git -C baselines/mem0 checkout 71fba8d46436f88569d600f81a55208c38ad30b5
git clone https://github.com/Nonlotus-eater/ami-reproduction.git baselines/ami-reproduction
git -C baselines/ami-reproduction checkout e545fdb10ce139bb19dc7c6f814ef853ed19cba0
git -C baselines/ami-reproduction apply ../../patches/ami-system-role.patch
git clone https://github.com/wenxiaof345-ctrl/vanilla-rag-memory.git baselines/InvMem
git -C baselines/InvMem checkout 31ab7bf9cfa3ee3c4f986e82f6e7a00b134ba8ca
git -C baselines/InvMem apply ../../patches/invmem-native-interface.patch
git clone https://github.com/imlrz/ReFind.git baselines/ReFind
git -C baselines/ReFind checkout a80175ca0eeb52a938d7cab7a602bc780de8a577
git -C baselines/ReFind apply ../../patches/refind-native-interface.patch

python3.11 -m venv baselines/mem0/.venv
baselines/mem0/.venv/bin/python -m pip install -e baselines/mem0 -e '.[api]' \
  'fastembed==0.7.4' 'openai==2.3.0' 'httpx==0.28.1' 'numpy==2.2.6'
python3.11 -m venv baselines/ami-reproduction/.venv
baselines/ami-reproduction/.venv/bin/python -m pip install \
  -r baselines/ami-reproduction/requirements-academic.lock.txt -e '.[api]'
python3.11 -m venv baselines/InvMem/.benchmark-env
baselines/InvMem/.benchmark-env/bin/python -m pip install -e '.[api]' \
  'numpy==2.2.6' 'faiss-cpu==1.15.1' 'fastapi==0.141.1' 'pydantic==2.13.4' 'httpx==0.28.1'
python3.11 -m venv baselines/ReFind/.benchmark-env
baselines/ReFind/.benchmark-env/bin/python -m pip install -e '.[api]' \
  'fastapi==0.141.1' 'pydantic==2.13.4' 'httpx==0.28.1'
```

可用 `--ami-root ..` 指向 fork 根 AMI，前提是确认其固定版本/兼容补丁；默认独立 clone 可保留根目录原状。
InvMem adapter 把其 `src/` 加入方法进程路径，注入共享 API 向量，未启用本地神经模型，不需安装上游 Torch/SentenceTransformer 全量可选依赖。ReFind 保留 agent/planner。
Mem0 的 FastEmbed 用于原生 BM25，不能与 LLMLingua 混淆；首次配置应准备 `models/fastembed` 缓存：

```bash
baselines/mem0/.venv/bin/python - <<'PY'
from pathlib import Path
from fastembed import SparseTextEmbedding
cache = Path('models/fastembed').resolve()
cache.mkdir(parents=True, exist_ok=True)
encoder = SparseTextEmbedding(model_name='Qdrant/bm25', cache_dir=str(cache))
assert len(next(encoder.embed(['Alice lives in Kyoto.'])).indices) > 0
print('BM25 cache verified')
PY
```

上述准备可能下载依赖/缓存，不调用付费模型。保留各原生仓库作者归属与许可。InvMem 固定提交未发现根 LICENSE，不为它额外宣称许可证；本次分发小型适配代码和补丁，原生实现单独获取。

## 6. 本地配置和五轮数据

```bash
.venv/bin/python scripts/configure_release.py --output configs/local
.venv/bin/python scripts/download_public_datasets.py --root datasets/raw
```

下载脚本覆盖 CLBench、BEAM、PersonaMem v1/v2 的固定作者版本。LoCoMo-Refined 的 `conversations.jsonl` / `questions.jsonl`、LME 的 `longmemeval_s_cleaned.json` 另行准备，见 `datasets/README.md`。真实路径写进 `configs/local/suite.json`，不提交机器绝对路径。
来源 SHA-256 必须匹配 `configs/reproduction/frozen_samples.json` 的 210 项，仅有题目 ID 或旧运行输出不满足条件。

```bash
.venv/bin/python scripts/prepare_five_seeds.py \
  --suite configs/local/suite.json --runtime configs/local/runtime.json \
  --output runs/prepared-five-seeds
```

此步骤离线校验来源，恢复完整历史和评分目标，冻结同一批题目并核验逐轮数量；输出五个 `seed-*` prepared 及 `summary.json`，不调用 API。
当前发布默认重建固定五轮，不需要旧 99 题分卷或参考 Word。
`sample_five_seeds.py` 是原随机抽样算法的留档入口；重新抽样仍需其参考 prepared 和表三文档。发布包因此保留 `docs/references/AMI_Benchmark_99_reference.docx` 这一小型程序输入；它不是当前论文正文，也不是固定五轮运行的依赖。

## 7. 离线检查与真实执行

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m memory_benchmark demo --output runs/demo-new
baselines/InvMem/.benchmark-env/bin/python integrations/native_bridge/test_adapters.py baselines/InvMem invmem
baselines/ReFind/.benchmark-env/bin/python integrations/native_bridge/test_adapters.py baselines/ReFind refind
```

adapter 检查禁止联网，以替身验证原生 API、system 消息、时间、检索和用户隔离，不证明真实模型分数。
公共测试和现有方法环境通过不等同于全新服务器依赖安装验收。
2026-10-17 已另行完成远端新 clone、五套全新依赖环境和四方法真实 API 配置检查，范围与结果见 [配置验证记录](configuration_validation_20261017.md)。

获得真实配置测试授权后，可一次运行四方法的最小 API 链路：

```bash
.venv/bin/python scripts/release_smoke.py --output runs/real-api-config-new
```

它逐方法运行两个完整人工历史、三道 QA 的原生记忆、检索、Answer 和 Judge，检查 3/3 评分及非空检索；不运行正式五轮。失败日志保留在输出目录，不自动重试。

下面会调用真实 API，仅在用户要求评测时执行。每方法每轮用新输出目录；seed 17 示例，其余替换为 18–21：

```bash
# AMI；Mem0 改为 methods.mem0.json 和独立输出目录。
.venv/bin/python -m memory_benchmark.runtime.queue \
  --prepared runs/prepared-five-seeds/seed-17 \
  --methods-config configs/local/methods.activememoryindex.json \
  --output runs/ami-new/seed-17

# InvMem；ReFind 改为 methods.refind.json 和独立输出目录。
.venv/bin/python integrations/native_bridge/runner.py \
  --prepared runs/prepared-five-seeds-new/seed-17 \
  --methods-config configs/local/methods.invmem.json \
  --policy configs/local/embedding_policy.json \
  --output runs/invmem-new/seed-17
```

默认不启动五轮并发。`embedding_policy.json` 的 `{}` 表示不施加桥接节流；并发时可显式配置 `state_directory`、`minimum_interval_seconds=0.25`、`cooldown_seconds=30`，同 provider 的桥接队列共用状态目录。桥接不隐藏失败或自动重放请求。
两个入口均支持相同 prepared/config/output 加 `--resume`，失败任务另加 `--retry-failed`。先确认同目录无活动队列；输入、源码、provider identity 或 runtime 变化须另建运行，不能修改旧 hash 绕过检查。

## 8. 完成验收与本地结果定位

逐轮每方法必须 `expected=scored`（202/199/196/201/205）、`complete=true`，六数据集无缺项。每方法共 1,003 题次、112 个历史任务；四方法为 4,012 题次、448 个任务。
部分评分只报覆盖率，完整 Overall 为 null；API/解析失败不能计为方法零分。

截至本次核对，原工作区四个 `runs/current-{ami,mem0,invmem,refind}-five-seeds.json` 均指向 `runs/clbench-judge-reaudit-20261017-v1/` 下对应方法，五轮完整且 `acceptance.json` 为 passed。旧文档中的“AMI 后三轮进行中”和旧 Mem0 分数入口已过时。
该位置只供本地查验，不是运行依赖，不随本次源码发布。若发布成绩，应另导出经过核验的紧凑汇总和运行条件，不把旧分数与当前无截断配置混写。

接手模型以用户当前授权范围执行；配置文档本身不授权自动启动付费评测、恢复历史 LightMem 任务或 push。用户明确要求的“先私有仓库推送 → 重新 clone 真实配置测试 → 通过后同步 fork → 删除临时测试目录”流程见 `publication_scope.md`。
