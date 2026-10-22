#!/usr/bin/env bash
# 并发运行 benchmark：5 个 seed × 指定方法
#
# 用法:
#   ./scripts/run_concurrent.sh <method> [options]
#
# 参数:
#   method    方法名: activememoryindex | mem0 | invmem | refind
#
# 选项:
#   --seeds "17 18 19 20 21"   指定 seed 列表（默认全部 5 个）
#   --output-dir DIR           输出根目录（默认 runs/<method>）
#   --resume                   恢复中断的运行
#   --retry-failed             重试失败的 job
#   --dry-run                  只打印命令不执行
#
# 示例:
#   # AMI 5 seed 并发
#   ./scripts/run_concurrent.sh activememoryindex
#
#   # Mem0 只跑 seed 17 18
#   ./scripts/run_concurrent.sh mem0 --seeds "17 18"
#
#   # InvMem 恢复 + 重试失败
#   ./scripts/run_concurrent.sh invmem --resume --retry-failed
#
#   # ReFind 试运行（只打印命令）
#   ./scripts/run_concurrent.sh refind --dry-run

set -euo pipefail

# ============================================================
# 配置
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCHMARK_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$BENCHMARK_ROOT"

PYTHON=".venv/bin/python"
PREPARED_ROOT="runs/prepared-five-seeds"
CONFIGS_DIR="configs/local"
EMBEDDING_POLICY="$CONFIGS_DIR/embedding_policy.json"

# 代理配置：OpenAI 走代理，阿里云直连
export HTTP_PROXY="${HTTP_PROXY:-http://127.0.0.1:17890/}"
export HTTPS_PROXY="${HTTPS_PROXY:-http://127.0.0.1:17890/}"
export NO_PROXY="aliyuncs.com"
export no_proxy="aliyuncs.com"
# 清除 ALL_PROXY（socks:// scheme 不被 httpx 支持）
unset ALL_PROXY all_proxy

# 默认值
SEEDS=(17 18 19 20 21)
METHOD=""
OUTPUT_DIR=""
RESUME=false
RETRY_FAILED=false
DRY_RUN=false

# ============================================================
# 参数解析
# ============================================================

if [[ $# -lt 1 ]]; then
    echo "用法: $0 <method> [--seeds \"17 18 19 20 21\"] [--output-dir DIR] [--resume] [--retry-failed] [--dry-run]"
    echo ""
    echo "方法: activememoryindex | mem0 | invmem | refind"
    exit 1
fi

METHOD="$1"
shift

while [[ $# -gt 0 ]]; do
    case "$1" in
        --seeds)
            shift
            read -ra SEEDS <<< "$1"
            ;;
        --output-dir)
            shift
            OUTPUT_DIR="$1"
            ;;
        --resume)
            RESUME=true
            ;;
        --retry-failed)
            RETRY_FAILED=true
            ;;
        --dry-run)
            DRY_RUN=true
            ;;
        *)
            echo "未知参数: $1"
            exit 1
            ;;
    esac
    shift
done

# 默认输出目录
if [[ -z "$OUTPUT_DIR" ]]; then
    OUTPUT_DIR="runs/$METHOD"
fi

# 方法配置文件
METHODS_CONFIG="$CONFIGS_DIR/methods.$METHOD.json"
if [[ ! -f "$METHODS_CONFIG" ]]; then
    echo "错误: 方法配置文件不存在: $METHODS_CONFIG"
    exit 1
fi

# 判断运行入口
case "$METHOD" in
    activememoryindex|mem0)
        RUNNER="queue"
        ;;
    invmem|refind)
        RUNNER="native_bridge"
        ;;
    *)
        echo "错误: 未知方法: $METHOD"
        echo "支持的方法: activememoryindex | mem0 | invmem | refind"
        exit 1
        ;;
esac

# ============================================================
# 构建运行命令
# ============================================================

build_command() {
    local seed=$1
    local prepared="$PREPARED_ROOT/seed-$seed"
    local output="$OUTPUT_DIR/seed-$seed"
    local log="$OUTPUT_DIR/seed-$seed.log"

    local cmd=()

    if [[ "$RUNNER" == "queue" ]]; then
        cmd+=("$PYTHON" -m memory_benchmark.runtime.queue)
        cmd+=(--prepared "$prepared")
        cmd+=(--methods-config "$METHODS_CONFIG")
        cmd+=(--output "$output")
    else
        cmd+=("$PYTHON" integrations/native_bridge/runner.py)
        cmd+=(--prepared "$prepared")
        cmd+=(--methods-config "$METHODS_CONFIG")
        cmd+=(--policy "$EMBEDDING_POLICY")
        cmd+=(--output "$output")
    fi

    if $RESUME; then
        cmd+=(--resume)
    fi
    if $RETRY_FAILED; then
        cmd+=(--retry-failed)
    fi

    echo "${cmd[@]}"
}

# ============================================================
# 执行
# ============================================================

echo "============================================================"
echo "Benchmark 并发运行"
echo "============================================================"
echo "方法:           $METHOD"
echo "运行入口:       $RUNNER"
echo "Seeds:          ${SEEDS[*]}"
echo "方法配置:       $METHODS_CONFIG"
echo "输出目录:       $OUTPUT_DIR"
echo "Resume:         $RESUME"
echo "Retry failed:   $RETRY_FAILED"
echo "Dry run:        $DRY_RUN"
echo "代理:           HTTP_PROXY=$HTTP_PROXY NO_PROXY=$NO_PROXY"
echo "============================================================"
echo ""

# 检查是否有同名进程在运行
EXISTING_PIDS=$(pgrep -f "memory_benchmark.runtime.(queue|worker)" 2>/dev/null || true)
if [[ -n "$EXISTING_PIDS" && "$DRY_RUN" == false ]]; then
    echo "警告: 检测到正在运行的 benchmark 进程:"
    echo "$EXISTING_PIDS" | while read pid; do
        ps -p "$pid" -o pid=,args= 2>/dev/null | head -1
    done
    echo ""
    read -p "是否继续? (y/N) " -r
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "已取消"
        exit 0
    fi
    echo ""
fi

# 创建输出目录
mkdir -p "$OUTPUT_DIR"

# 记数
NUM_SEEDS=${#SEEDS[@]}
PIDS=()

echo "启动 $NUM_SEEDS 个并发进程..."
echo ""

for seed in "${SEEDS[@]}"; do
    prepared="$PREPARED_ROOT/seed-$seed"
    output="$OUTPUT_DIR/seed-$seed"
    log="$OUTPUT_DIR/seed-$seed.log"

    if [[ ! -d "$prepared" ]]; then
        echo "  seed-$seed: 跳过（prepared 不存在: $prepared）"
        continue
    fi

    # 清理旧的 queue.lock
    rm -f "$output/queue.lock" 2>/dev/null

    cmd=$(build_command "$seed")

    if $DRY_RUN; then
        echo "  seed-$seed [DRY RUN]:"
        echo "    $cmd"
        echo "    log: $log"
    else
        # 启动进程
        eval "nohup $cmd > '$log' 2>&1 &"
        pid=$!
        PIDS+=($pid)
        echo "  seed-$seed: PID $pid"
        echo "    命令: $cmd"
        echo "    日志: $log"
    fi
done

echo ""

if $DRY_RUN; then
    echo "[DRY RUN] 未实际执行"
    exit 0
fi

if [[ ${#PIDS[@]} -eq 0 ]]; then
    echo "没有启动任何进程"
    exit 1
fi

# 保存 PID 列表
echo "${PIDS[*]}" > "$OUTPUT_DIR/pids.txt"
echo "PID 列表已保存到: $OUTPUT_DIR/pids.txt"
echo ""
echo "============================================================"
echo "监控命令:"
echo "  # 查看所有进程状态"
echo "  ps -p \$(cat $OUTPUT_DIR/pids.txt) -o pid=,stat=,etime=,args="
echo ""
echo "  # 查看各 seed 进度"
echo "  for seed in ${SEEDS[*]}; do"
echo "    $PYTHON -c \"import json; p=json.load(open('$OUTPUT_DIR/seed-'\$seed'/progress.json')); print(f'seed-'\$seed': {p['status']} {p.get('completed_jobs',0)}/22 failed={p.get('failed_jobs',0)}')\""
echo "  done"
echo ""
echo "  # 实时查看某个 seed 的日志"
echo "  tail -f $OUTPUT_DIR/seed-17.log"
echo ""
echo "  # 停止所有进程"
echo "  kill \$(cat $OUTPUT_DIR/pids.txt)"
echo "============================================================"
