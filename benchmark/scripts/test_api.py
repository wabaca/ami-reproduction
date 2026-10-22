#!/usr/bin/env python3
"""测试 LLM 和 embedding API 连接的脚本。

用法:
    .venv/bin/python scripts/test_api.py                    # 测试 .env 中的配置
    .venv/bin/python scripts/test_api.py --retries 5        # 外层重试 5 次（SDK 内层不重试）
    .venv/bin/python scripts/test_api.py --infinite         # 无限重试直到成功
    .venv/bin/python scripts/test_api.py --interval 10      # 每 10 秒重试一次
    .venv/bin/python scripts/test_api.py --count 16         # test_llm 串行发送 16 个请求
    .venv/bin/python scripts/test_api.py --burst 16         # 并发发送 16 个短请求
"""
import argparse
import os
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed


def load_env(path):
    """读取 .env 文件到环境变量。"""
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if value[:1] in {"'", '"'} and value[-1:] == value[:1]:
            value = value[1:-1]
        os.environ[key.strip()] = value


def test_llm(timeout=30, count=1):
    """测试 LLM API。串行依次发送 count 个请求。

    注意：SDK 内层 max_retries 固定为 0，重试由外层 run_with_retry 统一控制，
    避免两层重试叠加导致总请求数相乘、退避不可控。
    """
    from openai import OpenAI

    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    api_key = os.environ.get("OPENAI_API_KEY", "")
    model = os.environ.get("AMI_LLM_MODEL", "gpt-4o-mini")

    print(f"  endpoint: {base_url}/chat/completions")
    print(f"  model:    {model}")
    print(f"  key:      {api_key[:8]}...{api_key[-4:]}")
    print(f"  count:    {count}")
    print(f"  timeout:  {timeout}s")

    client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0)

    results = []
    for i in range(count):
        start = time.time()
        try:
            r = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "Say 'hello' and nothing else."}],
                max_tokens=10,
                temperature=0,
            )
            elapsed = time.time() - start
            content = r.choices[0].message.content
            print(f"  ✅ 请求 {i+1:2d}/{count} ({elapsed:.1f}s): {content}")
            results.append((True, elapsed, content, None))
        except Exception as e:
            elapsed = time.time() - start
            err = f"{type(e).__name__}: {str(e)[:150]}"
            print(f"  ❌ 请求 {i+1:2d}/{count} ({elapsed:.1f}s): {err}")
            results.append((False, elapsed, None, err))

    success = sum(1 for ok, _, _, _ in results if ok)
    failed = count - success
    print(f"\n  汇总: {success}/{count} 成功, {failed} 失败")
    if success > 0:
        ok_times = [e for ok, e, _, _ in results if ok]
        print(f"  成功请求耗时: min={min(ok_times):.1f}s  max={max(ok_times):.1f}s  avg={sum(ok_times)/len(ok_times):.1f}s")

    return failed == 0


def test_embedding(timeout=30):
    """测试 embedding API。

    注意：SDK 内层 max_retries 固定为 0，重试由外层 run_with_retry 统一控制。
    """
    from openai import OpenAI

    base_url = os.environ.get("AMI_EMBED_BASE_URL", "")
    api_key = os.environ.get("AMI_EMBED_API_KEY", "")
    model = os.environ.get("AMI_EMBED_MODEL", "text-embedding-v4")
    dimensions = int(os.environ.get("AMI_EMBED_DIMENSIONS", "1024"))

    if not base_url or not api_key:
        print("  ⚠️  未配置 embedding API，跳过")
        return True

    print(f"  endpoint: {base_url}/embeddings")
    print(f"  model:    {model}")
    print(f"  key:      {api_key[:8]}...{api_key[-4:]}")
    print(f"  timeout:  {timeout}s")

    client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0)
    start = time.time()
    r = client.embeddings.create(model=model, input="hello world", dimensions=dimensions)
    elapsed = time.time() - start
    dim = len(r.data[0].embedding)
    print(f"  ✅ 成功 ({elapsed:.1f}s): dim={dim}")
    return True


def test_llm_burst(count=16, timeout=30):
    """并发发送 count 个短 LLM 请求。

    注意：SDK 内层 max_retries 固定为 0，重试由外层 run_with_retry 统一控制。
    """
    from openai import OpenAI

    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    api_key = os.environ.get("OPENAI_API_KEY", "")
    model = os.environ.get("AMI_LLM_MODEL", "gpt-4o-mini")

    print(f"  endpoint: {base_url}/chat/completions")
    print(f"  model:    {model}")
    print(f"  key:      {api_key[:8]}...{api_key[-4:]}")
    print(f"  并发数:   {count}")
    print(f"  timeout:  {timeout}s")

    client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0)

    prompts = [
        "Say 'hello' and nothing else.",
        "What is 1+1? Answer with just the number.",
        "Say 'yes' and nothing else.",
        "What is the capital of France? One word.",
        "Say 'no' and nothing else.",
        "What is 2+2? Answer with just the number.",
        "Say 'ok' and nothing else.",
        "What color is the sky? One word.",
    ]

    def send_one(i):
        prompt = prompts[i % len(prompts)]
        start = time.time()
        try:
            r = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=10,
                temperature=0,
            )
            elapsed = time.time() - start
            content = r.choices[0].message.content.strip()
            return i, True, elapsed, content, None
        except Exception as e:
            elapsed = time.time() - start
            return i, False, elapsed, None, f"{type(e).__name__}: {str(e)[:150]}"

    print(f"\n  发送 {count} 个并发请求...")
    start_all = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(send_one, i) for i in range(count)]
        for f in as_completed(futures):
            idx, ok, elapsed, content, error = f.result()
            results.append((idx, ok, elapsed, content, error))
            status = "✅" if ok else "❌"
            detail = content[:30] if ok else error
            print(f"  {status} 请求 {idx+1:2d}/{count} ({elapsed:.1f}s): {detail}")

    elapsed_all = time.time() - start_all
    success = sum(1 for _, ok, _, _, _ in results if ok)
    failed = count - success

    print(f"\n  汇总: {success}/{count} 成功, {failed} 失败, 总耗时 {elapsed_all:.1f}s")
    if success > 0:
        ok_times = [e for _, ok, e, _, _ in results if ok]
        print(f"  成功请求耗时: min={min(ok_times):.1f}s  max={max(ok_times):.1f}s  avg={sum(ok_times)/len(ok_times):.1f}s")

    return failed == 0



def run_with_retry(name, func, *, infinite=False, interval=5, max_retries=0, timeout=30, **kwargs):
    """运行测试，支持重试。

    重试职责说明：
    - 本函数（外层）独占重试逻辑，通过 ``max_retries`` 控制最多重试次数。
    - 被调用的测试函数内部 SDK ``max_retries`` 固定为 0，不进行二次重试，
      避免两层重试叠加导致总请求数相乘、退避不可控。

    参数:
        max_retries: 外层最多重试次数（实际尝试 = max_retries + 1）。
                     ``--infinite`` 时本参数被忽略。
        interval:    每次重试前固定等待秒数。
    """
    attempt = 0
    while True:
        attempt += 1
        print(f"\n[{name}] 尝试 #{attempt}" + (" (无限重试)" if infinite else ""))
        try:
            ok = func(timeout=timeout, **kwargs)
            if not ok:
                raise RuntimeError("部分请求失败")
            return True
        except Exception as e:
            print(f"  ❌ 失败: {type(e).__name__}: {str(e)[:200]}")

            if not infinite and attempt >= max_retries + 1:
                print(f"  已达最大尝试次数 ({attempt})，放弃")
                return False

            print(f"  等待 {interval}s 后重试...")
            time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description="测试 API 连接")
    parser.add_argument("--env", default=".env", help=".env 文件路径")
    parser.add_argument("--retries", type=int, default=0, help="外层重试次数（SDK 内层不重试）")
    parser.add_argument("--infinite", action="store_true", help="无限重试直到成功")
    parser.add_argument("--interval", type=float, default=5, help="重试间隔秒数")
    parser.add_argument("--timeout", type=int, default=30, help="请求超时秒数")
    parser.add_argument("--llm-only", action="store_true", help="只测试 LLM")
    parser.add_argument("--embed-only", action="store_true", help="只测试 embedding")
    parser.add_argument("--burst", type=int, default=0, help="并发发送 N 个短 LLM 请求")
    parser.add_argument("--count", type=int, default=1, help="test_llm 串行发送 N 个请求（默认 1）")
    args = parser.parse_args()

    # 加载 .env
    env_path = Path(args.env).resolve()
    if not env_path.exists():
        print(f"错误: {env_path} 不存在")
        return 1
    load_env(env_path)
    print(f"已加载: {env_path}")

    print(f"代理: {os.environ.get('HTTP_PROXY', '无')}  NO_PROXY={os.environ.get('NO_PROXY', '')}")
    if args.burst:
        print(f"模式: 并发 {args.burst} 个短请求  外层重试={args.retries}  超时={args.timeout}s")
    else:
        count_info = f"  count={args.count}" if args.count > 1 else ""
        print(f"模式: {'无限重试' if args.infinite else f'外层重试 {args.retries} 次'}  间隔 {args.interval}s  超时 {args.timeout}s{count_info}")

    results = {}

    # 并发 burst 测试
    if args.burst:
        results["LLM Burst"] = run_with_retry(
            "LLM Burst", test_llm_burst,
            infinite=args.infinite, interval=args.interval,
            max_retries=args.retries, timeout=args.timeout,
            count=args.burst,
        )
    else:
        # 测试 LLM
        if not args.embed_only:
            results["LLM"] = run_with_retry(
                "LLM", test_llm,
                infinite=args.infinite, interval=args.interval,
                max_retries=args.retries, timeout=args.timeout,
                count=args.count,
            )

        # 测试 embedding
        if not args.llm_only:
            results["Embedding"] = run_with_retry(
                "Embedding", test_embedding,
                infinite=args.infinite, interval=args.interval,
                max_retries=args.retries, timeout=args.timeout,
            )

    # 汇总
    print("\n" + "=" * 50)
    print("汇总")
    print("=" * 50)
    for name, ok in results.items():
        print(f"  {name}: {'✅ 通过' if ok else '❌ 失败'}")

    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
