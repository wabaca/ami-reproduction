#!/usr/bin/env bash
set -euo pipefail

export TZ="Asia/Shanghai"

# # 代理配置：OpenAI 走代理，阿里云直连
# export HTTP_PROXY="${HTTP_PROXY:-http://127.0.0.1:17890/}"
# export HTTPS_PROXY="${HTTPS_PROXY:-http://127.0.0.1:17890/}"
# export NO_PROXY="aliyuncs.com"
# export no_proxy="aliyuncs.com"
# # 清除 ALL_PROXY（socks:// scheme 不被 httpx 支持）
# unset ALL_PROXY all_proxy

# 1. 基础路径配置
BENCHMARK_DIR="/home/wubucheng/agent-memory/ami-reproduction/benchmark"
LOG_DIR="runs/ami"
SEEDS=(17 18 19 20 21)
# SEEDS=(17)

# 2. 进入项目目录并准备日志文件夹
cd "$BENCHMARK_DIR"
mkdir -p "$LOG_DIR"

# 检查虚拟环境解释器是否存在
if [ ! -x ".venv/bin/python" ]; then
    echo "❌ 错误: 未找到虚拟环境解释器 .venv/bin/python"
    exit 1
fi

echo "=========================================="
echo "🚀 启动 5 个 AMI Seed 并发任务"
echo "=========================================="

# 3. 循环启动各 Seed 任务
for seed in "${SEEDS[@]}"; do
    nohup .venv/bin/python -m memory_benchmark.runtime.queue \
        --prepared "runs/prepared-five-seeds/seed-${seed}" \
        --methods-config "configs/local/methods.activememoryindex.json" \
        --output "runs/ami/seed-${seed}" \
        > "${LOG_DIR}/seed-${seed}.log" 2>&1 &
    
    echo "✔ seed-${seed} 已提交运行 (PID: $!) | 日志: ${LOG_DIR}/seed-${seed}.log"
done

echo ""
echo "⏳ 等待 5 秒以确认各进程初始化状态..."
sleep 5

echo ""
echo "=========================================="
echo "📊 当前运行中的队列进程状态 (5秒后)"
echo "=========================================="

RUNNING_COUNT=$(pgrep -f "memory_benchmark.runtime.queue" | wc -l)
echo "存活进程数: ${RUNNING_COUNT}/${#SEEDS[@]}"
echo ""

# 格式化输出 PID 与启动参数
ps aux | grep "memory_benchmark.runtime.queue" | grep -v grep | awk '{printf "PID: %-8s CPU: %-5s MEM: %-5s CMD: %s\n", $2, $3, $4, $11" "$12" "$13" "$14" "$15}' || true

echo "=========================================="
if [ "$RUNNING_COUNT" -eq "${#SEEDS[@]}" ]; then
    echo "🎉 全部 5 个 Seed 均已在后台稳定运行。"
else
    echo "⚠️ 部分进程可能启动失败，请检查 ${LOG_DIR}/seed-*.log 中的错误日志。"
fi