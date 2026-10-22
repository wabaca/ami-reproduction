#!/usr/bin/env bash
# 清理 benchmark 相关进程
#
# 用法:
#   ./scripts/kill_benchmark.sh          # 杀掉所有 benchmark 进程
#   ./scripts/kill_benchmark.sh --check  # 只检查不杀
#   ./scripts/kill_benchmark.sh -9       # 强制杀（SIGKILL）

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCHMARK_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$BENCHMARK_ROOT"

SIGNAL="TERM"
CHECK_ONLY=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --check)
            CHECK_ONLY=true
            ;;
        -9|--kill)
            SIGNAL="KILL"
            ;;
        -h|--help)
            echo "用法: $0 [--check] [-9]"
            echo "  --check  只检查不杀"
            echo "  -9       强制杀（SIGKILL）"
            exit 0
            ;;
    esac
    shift
done

# 查找所有 benchmark 相关进程
echo "=== 查找 benchmark 进程 ==="
echo ""

# 1. 队列进程（queue）
QUEUE_PIDS=$(pgrep -f "memory_benchmark\.runtime\.queue" 2>/dev/null || true)
# 2. worker 进程
WORKER_PIDS=$(pgrep -f "memory_benchmark\.runtime\.worker" 2>/dev/null || true)
# 3. native_bridge runner 进程
BRIDGE_PIDS=$(pgrep -f "native_bridge/runner\.py" 2>/dev/null || true)
# 4. native_bridge worker 进程
BRIDGE_WORKER_PIDS=$(pgrep -f "native_bridge/worker\.py" 2>/dev/null || true)

ALL_PIDS=()
for pid in $QUEUE_PIDS $WORKER_PIDS $BRIDGE_PIDS $BRIDGE_WORKER_PIDS; do
    if [[ -n "$pid" ]]; then
        ALL_PIDS+=("$pid")
    fi
done

if [[ ${#ALL_PIDS[@]} -eq 0 ]]; then
    echo "没有发现 benchmark 进程"
    exit 0
fi

echo "发现 ${#ALL_PIDS[@]} 个进程:"
echo ""
printf "%-8s %-6s %-8s %-10s %s\n" "PID" "CPU%" "MEM%" "TIME" "类型/命令"
echo "------------------------------------------------------------------"

for pid in "${ALL_PIDS[@]}"; do
    if ps -p "$pid" > /dev/null 2>&1; then
        info=$(ps -p "$pid" -o pid=,pcpu=,pmem=,etime=,args= 2>/dev/null)
        pid_val=$(echo "$info" | awk '{print $1}')
        cpu=$(echo "$info" | awk '{print $2}')
        mem=$(echo "$info" | awk '{print $3}')
        etime=$(echo "$info" | awk '{print $4}')
        args=$(echo "$info" | cut -d' ' -f5-)

        # 判断类型
        if echo "$args" | grep -q "runtime\.queue"; then
            ptype="queue"
        elif echo "$args" | grep -q "runtime\.worker"; then
            ptype="worker"
        elif echo "$args" | grep -q "native_bridge/runner"; then
            ptype="bridge"
        elif echo "$args" | grep -q "native_bridge/worker"; then
            ptype="bridge_worker"
        else
            ptype="other"
        fi

        # 提取 seed 信息
        seed_info=""
        if echo "$args" | grep -qo "seed-[0-9]*"; then
            seed_info=$(echo "$args" | grep -o "seed-[0-9]*" | head -1)
        fi

        printf "%-8s %-6s %-8s %-10s [%s] %s\n" "$pid_val" "$cpu" "$mem" "$etime" "$ptype $seed_info" ""
    fi
done

echo ""

if $CHECK_ONLY; then
    echo "（--check 模式，不执行杀操作）"
    exit 0
fi

# 杀进程
echo "=== 杀掉进程（信号: SIG$SIGNAL）==="
for pid in "${ALL_PIDS[@]}"; do
    if ps -p "$pid" > /dev/null 2>&1; then
        kill "-$SIGNAL" "$pid" 2>/dev/null && echo "  killed PID $pid"
    fi
done

# 等待退出
sleep 2

# 检查是否还有残留
REMAINING=0
for pid in "${ALL_PIDS[@]}"; do
    if ps -p "$pid" > /dev/null 2>&1; then
        REMAINING=$((REMAINING + 1))
    fi
done

if [[ $REMAINING -gt 0 ]]; then
    echo ""
    echo "警告: $REMAINING 个进程仍在运行，尝试强制杀..."
    for pid in "${ALL_PIDS[@]}"; do
        if ps -p "$pid" > /dev/null 2>&1; then
            kill -9 "$pid" 2>/dev/null && echo "  force killed PID $pid"
        fi
    done
    sleep 1
fi

# 清理 queue.lock 文件
echo ""
echo "=== 清理 queue.lock ==="
LOCK_COUNT=$(find runs/ -name "queue.lock" -delete 2>/dev/null | wc -l)
# find -delete 不输出，用另一种方式
find runs/ -name "queue.lock" 2>/dev/null | while read f; do
    rm -f "$f"
done
echo "已清理"

# 最终确认
echo ""
echo "=== 最终确认 ==="
FINAL=$(pgrep -f "memory_benchmark\.(runtime\.queue|runtime\.worker)|native_bridge/(runner|worker)\.py" 2>/dev/null | wc -l)
if [[ $FINAL -eq 0 ]]; then
    echo "✓ 所有 benchmark 进程已停止"
else
    echo "✗ 仍有 $FINAL 个进程在运行:"
    pgrep -f "memory_benchmark\.(runtime\.queue|runtime\.worker)|native_bridge/(runner|worker)\.py" 2>/dev/null | while read pid; do
        ps -p "$pid" -o pid=,args= 2>/dev/null
    done
fi
