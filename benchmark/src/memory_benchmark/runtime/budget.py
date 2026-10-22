from __future__ import annotations
import json
import math
import re
import threading
import time

class CallBudget:
    """Record token usage and estimated costs; no request-count or spending caps.

    Prices must be the user's provider rates (CNY per million tokens), including
    any applicable context tier. UTF-8 bytes plus overhead conservatively reserve
    input costs for diagnostics. Estimates never prevent a request.
    """
    def __init__(self, profile, *, on_change=None):
        self.lock = threading.RLock()
        self.rows = []
        self.reserved = 0.0
        self.failed = None
        self.on_change = on_change
        self.peak_reserved = 0.0
        self.allow_retries = profile.get('AMI_ALLOW_RETRIES', '0') == '1'
        self.rates = {}
        for kind, names in {
            'llm': ('AMI_TEST_INPUT_CNY_PER_MILLION', 'AMI_TEST_OUTPUT_CNY_PER_MILLION'),
            'embedding': ('AMI_TEST_EMBED_CNY_PER_MILLION',),
        }.items():
            values = [float(profile.get(name, 'nan')) for name in names]
            if any(not math.isfinite(v) or v <= 0 for v in values):
                raise ValueError('Set positive provider prices: ' + ', '.join(names))
            self.rates[kind] = values
        if 'AMI_TEST_JUDGE_INPUT_CNY_PER_MILLION' in profile:
            values = [float(profile.get(name, 'nan')) for name in
                      ['AMI_TEST_JUDGE_INPUT_CNY_PER_MILLION', 'AMI_TEST_JUDGE_OUTPUT_CNY_PER_MILLION']]
            if any(not math.isfinite(v) or v <= 0 for v in values):
                raise ValueError('Set positive judge provider prices')
            self.rates['judge'] = values

    def check(self):
        if self.failed:
            self.notify()
            raise RuntimeError(self.failed)

    def notify(self):
        if self.on_change is not None:
            self.on_change()

    def cost(self, kind, input_tokens, output_tokens=0):
        rates = self.rates[kind]
        return (input_tokens * rates[0] +
                (output_tokens * rates[1] if len(rates) > 1 else 0)) / 1_000_000

def make_transport(budget, kind, endpoint, model, *,
                   disable_thinking_required=True, allowed_response_models=None, timeout=30,
                   embedding_dimensions=None, embedding_batch=10):
    import httpx
    allowed_response_models = frozenset(allowed_response_models or {model})

    class GuardedTransport(httpx.BaseTransport):
        def __init__(self):
            self.client = httpx.Client(timeout=timeout, follow_redirects=False)

        def close(self):
            self.client.close()

        def handle_request(self, request):
            import httpx as _httpx
            # HTTP 状态码视为可重试的集合：429 限流、5xx 服务端临时错误。
            RETRYABLE_STATUS = {429, 500, 502, 503, 504}
            with budget.lock:
                budget.check()
                payload = json.loads(request.content)
                if request.method != 'POST' or str(request.url) != endpoint or payload.get('model') != model:
                    budget.failed = 'Unexpected provider endpoint or model'
                    budget.check()
                if request.headers.get('x-stainless-retry-count', '0') != '0' and not budget.allow_retries:
                    budget.failed = 'Retry forbidden'
                    budget.check()
                if kind in {'llm', 'judge'}:
                    if (disable_thinking_required and payload.get('enable_thinking') is not False) or payload.get('stream'):
                        budget.failed = 'Contract requires nonthinking, nonstreaming calls'
                        budget.check()
                    messages = payload['messages']
                    input_bound = sum(len(m['content'].encode('utf-8')) + 256 for m in messages) + 256
                    output_bound = payload['max_tokens']
                else:
                    inputs = payload.get('input')
                    if (not isinstance(inputs, list) or not 1 <= len(inputs) <= embedding_batch
                            or any(not isinstance(t, str) or not t for t in inputs)
                            or (embedding_dimensions is not None and payload.get('dimensions') != embedding_dimensions)):
                        budget.failed = 'Invalid embedding input, batch size or dimensions'
                        budget.check()
                    input_bound = sum(len(text.encode('utf-8')) + 256 for text in payload['input'])
                    output_bound = 0
                reservation = budget.cost(kind, input_bound, output_bound)
                budget.reserved += reservation
                budget.peak_reserved = max(budget.peak_reserved, budget.reserved)
                retry_count = int(request.headers.get('x-stainless-retry-count', '0'))
                row = {'kind': kind, 'input_token_bound': input_bound,
                       'output_token_bound': output_bound, 'reserved_cny': reservation,
                       'requested_model': payload['model'],
                       'phase': getattr(budget, 'phase', 'unspecified'), 'started_unix_seconds': time.time(),
                       'retry_count': retry_count}
                request_started = time.perf_counter()
                budget.rows.append(row)
                budget.notify()
                response = None
                try:
                    try:
                        response = self.client.send(request, stream=False, follow_redirects=False)
                        response.read()
                    except _httpx.TransportError as transport_error:
                        # 网络层错误（连接超时、DNS、连接重置等）：不熔断 budget，
                        # 让 SDK 的 max_retries 机制真正重传。重试时 budget.check() 仍通过。
                        row['error_type'] = type(transport_error).__name__
                        row['seconds'] = time.perf_counter() - request_started
                        budget.reserved += row.get('estimated_cny', 0) - reservation
                        budget.notify()
                        raise
                    row['http_status'] = response.status_code
                    if response.status_code != 200:
                        try:
                            error = response.json().get('error', {})
                            code = error.get('code') if isinstance(error, dict) else None
                        except (ValueError, AttributeError):
                            code = None
                        # Retain known error codes, never arbitrary provider bodies.
                        safe_codes = {'model_not_found', 'ModelNotFound', 'invalid_api_key',
                            'InvalidApiKey', 'rate_limit_exceeded', 'insufficient_quota',
                            'WorkSpaceNotFound', 'model_not_supported', 'NotFound',
                            'RequestPath.NotFound', 'invalid_parameter', 'InvalidParameter'}
                        if isinstance(code, str) and code in safe_codes:
                            row['provider_error_code'] = code
                        if response.status_code in RETRYABLE_STATUS:
                            # 429/5xx：可重试，不熔断 budget，让 SDK 重传。
                            row['retryable'] = True
                            budget.reserved += row.get('estimated_cny', 0) - reservation
                            budget.notify()
                            raise RuntimeError(f'Provider returned retryable status {response.status_code}')
                        # 4xx 业务错误：致命，熔断。
                        budget.failed = f"{kind} HTTP {response.status_code}" + (
                            f" ({code})" if row.get('provider_error_code') else '')
                        raise RuntimeError('Provider returned non-200 status')
                    body = response.json()
                    if body.get('error'):
                        raise ValueError('Provider error response')
                    usage = body['usage']
                    input_tokens = usage['prompt_tokens']
                    output_tokens = usage.get('completion_tokens', 0)
                    if any(type(v) is not int or v < 0 for v in [input_tokens, output_tokens]):
                        raise ValueError('Invalid token usage')
                    row['input_tokens'], row['output_tokens'] = input_tokens, output_tokens
                    row['estimated_cny'] = budget.cost(kind, input_tokens, output_tokens)
                    if kind in {'llm', 'judge', 'embedding'}:
                        returned_model = body.get('model')
                        if isinstance(returned_model, str) and re.fullmatch(r'[A-Za-z0-9_.:/-]{1,128}', returned_model):
                            row['returned_model'] = returned_model
                        if returned_model not in allowed_response_models:
                            row['validation_error'] = 'response_model_missing' if returned_model is None else 'response_model_not_allowed'
                            budget.failed = 'Response model missing or outside frozen allowlist'
                            raise ValueError('Response model does not match selected model or allowed snapshot')
                    if kind == 'embedding':
                        from .embedding import normalized
                        data = body.get('data')
                        if not isinstance(data, list) or len(data) != len(payload['input']):
                            raise ValueError('Incomplete embedding response')
                        indices = [item.get('index') for item in data if isinstance(item, dict)]
                        if (len(indices) != len(data) or any(type(i) is not int for i in indices)
                                or sorted(indices) != list(range(len(data)))):
                            raise ValueError('Invalid embedding response indices')
                        for item in data:
                            normalized(item.get('embedding'), embedding_dimensions or len(item.get('embedding', [])))
                        row['dimensions'] = embedding_dimensions
                        row['inputs'] = len(payload['input'])
                    if input_tokens > input_bound or output_tokens > output_bound:
                        raise ValueError('Provider usage exceeds reserved bounds')
                    if kind in {'llm', 'judge'} and not body['choices'][0]['message']['content'].strip():
                        raise ValueError('Empty completion')
                except Exception as error:
                    # 仅在 budget.failed 已被置位（致命错误）时熔断；
                    # 可重试错误（网络错误、429/5xx）不置位，让 SDK 重传。
                    row['error_type'] = type(error).__name__
                    if 'seconds' not in row:
                        row['seconds'] = time.perf_counter() - request_started
                    if response is not None:
                        response.close()
                    if budget.failed is None:
                        # 如果是可重试错误（row 标记了 retryable），不熔断。
                        if not row.get('retryable') and not isinstance(error, _httpx.TransportError):
                            budget.failed = 'Provider failure; no further requests allowed'
                    if budget.failed is not None:
                        budget.check()
                # Keep actual completed costs, plus any in-flight reservation,
                # rather than charging every conservative estimate forever.
                budget.reserved += row.get('estimated_cny', 0) - reservation
                if 'seconds' not in row:
                    row['seconds'] = time.perf_counter() - request_started
                budget.notify()
                return response

    return GuardedTransport()
