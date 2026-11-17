#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "opentelemetry-sdk==1.45.2",
#   "opentelemetry-exporter-otlp-proto-http==1.45.2",
# ]
# ///
"""依赖升级 PR 评审：pi（coding agent）判断升级在本仓库的用法下会不会坏，结论贴到 PR。

  uv run --script review.py                                  CI（action.yml）
  uv run --script review.py --repo o/r --pr 748 --dry-run    本地：结论打印到 stdout

模型只有两个工具：在 OpenSandbox Kata VM 里执行的 bash（extension.ts）与 submit_review。VM 里是
PR head 的源码，出网挡掉内网；网关 key 与 sandbox key 只在 runner 上的 pi 进程里，模型碰不到。

报告模式：只评论、不合并，也不让 job 失败——Renovate 的 automerge 要求全部 check 通过，
评审失败不能挡住它。

每次评审是一条 trace：根 span 下每轮模型调用与每次工具调用各一个 span，traceparent 经
models.json 的请求头带给 AI 网关，网关的 GenAI span 落在同一条 trace 里。
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'security-scan'))
from pr_comment import MAX_COMMENT, request  # noqa: E402

MARKER = '<!-- upgrade-review -->'
# OpenAI 兼容端点（homelab-infra apps/ai-gateway/README.md）；gpt-* 走 ChatGPT 订阅。
GATEWAY = 'https://ai.lab.ddupan.top/v1'
MODEL = os.environ.get('UPGRADE_REVIEW_MODEL', 'gpt-6-luna')
# kv/k8s/ai-gateway-keys/<id>，由 key UI 签发；吊销即停用评审。
KEY_ID = os.environ.get('UPGRADE_REVIEW_KEY_ID', 'upgrade-agent')
SANDBOX_API = os.environ.get('UPGRADE_REVIEW_SANDBOX_API', 'http://sandbox-k8s.lab.ddupan.top:8080')
SANDBOX_IMAGE = os.environ.get('UPGRADE_REVIEW_SANDBOX_IMAGE') or (
    'docker.io/gitea/runner-images:ubuntu-latest'
    '@sha256:fd911d7417bfbf0f454530e447da95b58001e1df41bbc5e1a8dd35d432575aae')
# 内网一律不通：LAN、集群 Pod/Service、CGNAT、云元数据。公网放行。
PRIVATE = ['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '100.64.0.0/10', '169.254.0.0/16']
TOOLS = 'bash,submit_review'
# 一次评审的墙钟上限。超时即杀掉 pi，按失败处理；sandbox 多留几分钟后自行过期。
TIMEOUT = 900
LABEL = {'safe': '✅ safe', 'needs_review': '⚠️ needs_review', 'unsafe': '❌ unsafe'}
# CI 的 pod job 跑在 sandbox 集群，解析不了 homelab 的 Service 名：走 LAN 入口（collector 的
# 4319，bearer token，只进 traces 管道）。
OTLP = os.environ.get('OTEL_EXPORTER_OTLP_TRACES_ENDPOINT', 'https://otlp.lab.ddupan.top/v1/traces')


def bao_kv(path, field):
    """本地运行时从环境变量取（UPGRADE_REVIEW_<FIELD>），CI 里用 spiffe-openbao-login 的 token 读 Bao。"""
    try:
        data = request(f"{os.environ['BAO_ADDR'].rstrip('/')}/v1/kv/data/{path}",
                       headers={'X-Vault-Token': os.environ['BAO_TOKEN']})['data']['data']
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'读取 kv/{path} 失败：HTTP {e.code}') from None
    return data[field]


def secret(env, path, field):
    return os.environ.get(env) or bao_kv(path, field)


def fetch_text(url, auth):
    with urllib.request.urlopen(urllib.request.Request(url, headers=auth), timeout=30) as r:
        return r.read().decode(errors='replace')


def build_context(api, auth, pr, extra_marker):
    """agent 的初始输入：PR 描述、diff，和仓库 CI 贴出的额外上下文（可选）。"""
    diff = fetch_text(f"{api}/pulls/{pr['number']}.diff", auth)
    files = sorted({line[6:] for line in diff.splitlines() if line.startswith('+++ b/')})
    ctx = (f"<pr_title>{pr['title']}</pr_title>\n\n"
           f"<changed_files>\n" + '\n'.join(files) + "\n</changed_files>\n\n"
           f"<pr_description>\n{pr.get('body') or ''}\n</pr_description>\n\n"
           f"<pr_diff>\n{diff}\n</pr_diff>\n")
    if extra_marker:
        comments = request(f"{api}/issues/{pr['number']}/comments", headers=auth)
        extra = next((c['body'] for c in comments if c['body'].startswith(extra_marker)),
                     f'（没有 {extra_marker} 评论：本 PR 未触发，或尚未完成）')
        ctx += f"\n<ci_context>\n{extra}\n</ci_context>\n"
    return ctx


class Sandbox:
    """一次性 Kata VM。源码以 tarball 上传，用完删除；job 被杀时靠 timeout 过期回收。"""

    def __init__(self, key, labels):
        self.h = {'OPEN-SANDBOX-API-KEY': key}
        body = {'image': {'uri': SANDBOX_IMAGE}, 'entrypoint': ['sleep', 'infinity'],
                'timeout': TIMEOUT + 300, 'resourceLimits': {'cpu': '2', 'memory': '3Gi'},
                'metadata': {'purpose': 'upgrade-review', **labels},
                'networkPolicy': {'defaultAction': 'allow',
                                  'egress': [{'action': 'deny', 'target': c} for c in PRIVATE]}}
        self.id = request(f'{SANDBOX_API}/v1/sandboxes', 'POST', body, self.h)['id']
        self.execd = f'{SANDBOX_API}/v1/sandboxes/{self.id}/proxy/44772'
        deadline = time.monotonic() + 300
        while (state := request(f'{SANDBOX_API}/v1/sandboxes/{self.id}', headers=self.h)['status']['state']) != 'Running':
            if state == 'Failed' or time.monotonic() > deadline:
                raise RuntimeError(f'sandbox {self.id} 未就绪：{state}')
            time.sleep(3)

    def upload(self, local, remote):
        """execd 的 multipart 上传：metadata 必须是文件 part。"""
        b = uuid.uuid4().hex
        meta = json.dumps({'path': remote, 'mode': 644}).encode()
        data = (f'--{b}\r\nContent-Disposition: form-data; name="metadata"; filename="metadata.json"\r\n'
                f'Content-Type: application/json\r\n\r\n').encode() + meta + (
                f'\r\n--{b}\r\nContent-Disposition: form-data; name="file"; filename="repo.tgz"\r\n'
                f'Content-Type: application/octet-stream\r\n\r\n').encode() + Path(local).read_bytes() + f'\r\n--{b}--\r\n'.encode()
        req = urllib.request.Request(f'{self.execd}/files/upload', data=data, method='POST',
                                     headers={**self.h, 'Content-Type': f'multipart/form-data; boundary={b}'})
        urllib.request.urlopen(req, timeout=120).close()

    def run(self, command):
        req = urllib.request.Request(f'{self.execd}/command', method='POST',
                                     data=json.dumps({'command': command, 'cwd': '/', 'timeout': 120000}).encode(),
                                     headers={**self.h, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=150) as r:
            for line in r:
                e = json.loads(line) if line.strip() else {}
                if e.get('type') == 'error':
                    raise RuntimeError(f'sandbox 命令失败：{command}：{e["error"]}')

    def delete(self):
        try:
            request(f'{SANDBOX_API}/v1/sandboxes/{self.id}', 'DELETE', headers=self.h)
        except Exception as e:  # 删不掉也会按 timeout 过期
            print(f'::warning::删除 sandbox {self.id} 失败：{e!r}')


def traceparent(span):
    c = span.get_span_context()
    return f'00-{c.trace_id:032x}-{c.span_id:016x}-01'


def run_agent(context, key, sandbox, sandbox_key, tracer, root):
    """运行 pi，把事件流转成 span；返回 (submit_review 的参数或 None, 统计)。"""
    stats = {'turns': 0, 'input': 0, 'output': 0, 'cache_read': 0, 'tools': {}}
    review = None
    with tempfile.TemporaryDirectory() as tmp:
        agent_dir = Path(tmp, 'agent')
        agent_dir.mkdir()
        (agent_dir / 'models.json').write_text(json.dumps({'providers': {'gateway': {
            'baseUrl': GATEWAY, 'api': 'openai-completions', 'authHeader': True,
            # 值由 pi 在请求时从环境变量解析，key 不落盘。
            'apiKey': '$AI_GATEWAY_KEY', 'headers': {'traceparent': '$TRACEPARENT'},
            'models': [{'id': MODEL}],
        }}}))
        ctx_file = Path(tmp, 'context.md')
        ctx_file.write_text(context)
        env = {
            'PATH': os.environ['PATH'], 'HOME': tmp, 'LANG': 'C.UTF-8',
            'AI_GATEWAY_KEY': key, 'TRACEPARENT': traceparent(root),
            'SANDBOX_EXECD_URL': sandbox.execd, 'SANDBOX_API_KEY': sandbox_key,
            'PI_CODING_AGENT_DIR': str(agent_dir),
            # 不访问 pi.dev：无版本检查、无安装统计、无模型目录刷新。
            'PI_OFFLINE': '1', 'PI_TELEMETRY': '0', 'PI_SKIP_VERSION_CHECK': '1',
        }
        cmd = [str(HERE / 'node_modules/.bin/pi'), '--model', f'gateway/{MODEL}', '--no-session',
               '--no-skills', '--no-extensions', '-e', str(HERE / 'extension.ts'), '--tools', TOOLS,
               '--append-system-prompt', str(HERE / 'prompt.md'), '--mode', 'json',
               f'@{ctx_file}', '开始评审。']
        # stdin 必须关掉：非终端 stdin 会被 pi 当成管道输入一直等待。
        # stderr 写文件：用管道时没人读，写满缓冲区会让 pi 卡住。cwd 是空目录：模型的工具都在 sandbox 里。
        err_file = Path(tmp, 'pi.err')
        with err_file.open('w') as err:
            proc = subprocess.Popen(cmd, env=env, cwd=tmp, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=err, text=True)
        timer = threading.Timer(TIMEOUT, proc.kill)
        timer.start()
        parent = trace.set_span_in_context(root)
        turn, tools = None, {}
        try:
            for line in proc.stdout:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = e.get('type')
                if t == 'turn_start':
                    stats['turns'] += 1
                    turn = tracer.start_span(f"turn {stats['turns']}", context=parent)
                elif t == 'turn_end' and turn:
                    m = e.get('message') or {}
                    u = m.get('usage') or {}
                    stats['input'] += u.get('input', 0)
                    stats['output'] += u.get('output', 0)
                    stats['cache_read'] += u.get('cacheRead', 0)
                    turn.set_attributes({
                        'gen_ai.request.model': MODEL, 'gen_ai.response.model': m.get('model', ''),
                        'gen_ai.usage.input_tokens': u.get('input', 0),
                        'gen_ai.usage.output_tokens': u.get('output', 0),
                        'gen_ai.usage.cache_read.input_tokens': u.get('cacheRead', 0),
                        'pi.stop_reason': m.get('stopReason', ''),
                    })
                    turn.end()
                    turn = None
                elif t == 'tool_execution_start':
                    name = e.get('toolName', '')
                    stats['tools'][name] = stats['tools'].get(name, 0) + 1
                    args = e.get('args') or {}
                    if name == 'submit_review':
                        review = args
                    span = tracer.start_span(f'tool {name}', context=trace.set_span_in_context(turn or root))
                    span.set_attributes({'gen_ai.tool.name': name,
                                         'pi.tool.args': json.dumps(args, ensure_ascii=False)[:1000]})
                    tools[e.get('toolCallId')] = span
                elif t == 'tool_execution_end' and (span := tools.pop(e.get('toolCallId'), None)):
                    if e.get('isError'):
                        result = e.get('result') or {}
                        text = ''.join(c.get('text', '') for c in result.get('content') or [] if isinstance(c, dict))
                        span.set_status(Status(StatusCode.ERROR, text[:300]))
                    span.end()
        finally:
            timer.cancel()
            for span in [*tools.values(), *([turn] if turn else [])]:
                span.end()
            proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f'pi 退出码 {proc.returncode}：{err_file.read_text()[-2000:]}')
    return review, stats


def render(review, stats, trace_id):
    lines = [f"## 升级评审：{LABEL[review['verdict']]}", '', review['summary'], '']
    for title, key in (('与本仓库用法相关的破坏性变更', 'breaking_changes'), ('风险与不确定', 'risks'),
                       ('合并前建议确认', 'human_checks')):
        if review.get(key):
            lines += [f'### {title}', ''] + [f'- {x}' for x in review[key]] + ['']
    if review.get('sources'):
        lines += ['<details><summary>查阅来源</summary>', ''] + [f'- {x}' for x in review['sources']] + ['', '</details>', '']
    tools = '、'.join(f'{k}×{v}' for k, v in sorted(stats['tools'].items()))
    lines.append(f"<sub>{MODEL} · {stats['turns']} 轮 · 输入 {stats['input']}（缓存 {stats['cache_read']}）/ "
                 f"输出 {stats['output']} tokens · 工具 {tools} · trace {trace_id} · "
                 '报告模式：仅供参考，不会自动合并。</sub>')
    return '\n'.join(lines)


def post_comment(api, number, body, token):
    body = MARKER + '\n' + body
    if len(body) > MAX_COMMENT:
        body = body[:MAX_COMMENT] + '\n\n…已截断。'
    auth = {'Authorization': f'token {token}'}
    issue = f'{api}/issues/{number}/comments'
    # ponytail: 只看第一页评论（默认 50 条），评论更多的 PR 会新建而非更新
    mine = [c for c in request(issue, headers=auth) if c['body'].startswith(MARKER)]
    if mine:
        request(f"{api}/issues/comments/{mine[0]['id']}", 'PATCH', {'body': body}, auth)
    else:
        request(issue, 'POST', {'body': body}, auth)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', help='本地运行：owner/repo（默认 GITHUB_REPOSITORY）')
    parser.add_argument('--pr', type=int, help='本地运行：PR 编号（默认取 GITHUB_EVENT_PATH）')
    parser.add_argument('--source', default='.', help='上传到 sandbox 的仓库检出（git archive HEAD）')
    parser.add_argument('--extra-context-marker', default=os.environ.get('UPGRADE_REVIEW_EXTRA_CONTEXT_MARKER', ''),
                        help='把以这个 HTML 注释开头的 PR 评论（如 CI 贴的渲染 diff）一并交给 agent')
    parser.add_argument('--dry-run', action='store_true', help='结论打印到 stdout，不评论')
    opts = parser.parse_args()

    server = os.environ.get('GITHUB_SERVER_URL', 'https://git.ddupan.top')
    api = f"{server}/api/v1/repos/{opts.repo or os.environ['GITHUB_REPOSITORY']}"
    token = os.environ.get('GITEA_TOKEN')
    auth = {'Authorization': f'token {token}'} if token else {}
    number = opts.pr or json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())['pull_request']['number']
    # 不用事件 payload 里的 PR：Renovate 先推 commit 再改标题与描述，payload 是旧的。
    pr = request(f'{api}/pulls/{number}', headers=auth)

    provider = TracerProvider(resource=Resource.create({'service.name': 'upgrade-review'}))
    try:
        if otlp := secret('UPGRADE_REVIEW_OTLP_TOKEN', 'k8s/otel-collector', 'public_token'):
            provider.add_span_processor(BatchSpanProcessor(
                OTLPSpanExporter(endpoint=OTLP, headers={'Authorization': f'Bearer {otlp}'})))
    except Exception as e:  # 拿不到就不导出 trace，评审照常
        print(f'::warning::读取 OTLP token 失败，不导出 trace：{e!r}')
    tracer = provider.get_tracer('upgrade-review')
    sandbox = None
    try:
        with tracer.start_as_current_span('upgrade_review') as root:
            root.set_attributes({'vcs.repository.name': api.split('/repos/')[1], 'vcs.change.id': str(number),
                                 'vcs.change.title': pr['title'], 'gen_ai.request.model': MODEL})
            context = build_context(api, auth, pr, opts.extra_context_marker)
            gateway_key = secret('UPGRADE_REVIEW_API_KEY', f'k8s/ai-gateway-keys/{KEY_ID}', 'key')
            sandbox_key = secret('UPGRADE_REVIEW_SANDBOX_KEY', 'k8s/opensandbox-api', 'api_key')
            with tracer.start_as_current_span('sandbox setup'):
                sandbox = Sandbox(sandbox_key, {'repo': api.split('/repos/')[1].replace('/', '.'), 'pr': str(number)})
                with tempfile.TemporaryDirectory() as tmp:
                    tgz = Path(tmp, 'repo.tgz')
                    subprocess.run(['git', '-C', opts.source, 'archive', '--format=tar.gz', '-o', str(tgz), 'HEAD'], check=True)
                    sandbox.upload(tgz, '/tmp/repo.tgz')
                sandbox.run('mkdir -p /workspace && tar -xzf /tmp/repo.tgz -C /workspace && rm /tmp/repo.tgz')
            review, stats = run_agent(context, gateway_key, sandbox, sandbox_key, tracer, root)
            if not review:
                raise RuntimeError(f'agent 未调用 submit_review（{stats}）')
            root.set_attribute('upgrade_review.verdict', review['verdict'])
            body = render(review, stats, f'{root.get_span_context().trace_id:032x}')
    finally:
        if sandbox:
            sandbox.delete()
        provider.shutdown()  # 导出失败只记日志，不影响评审结果
    if opts.dry_run:
        print(body)
        return
    # 评论以独立的 Bot 账号 upgrade-agent 发出（README 的 Bootstrap）；读不到时退回 job token。
    try:
        bot = secret('UPGRADE_REVIEW_COMMENT_TOKEN', 'k8s/upgrade-agent', 'token')
    except Exception as e:
        print(f'::warning::读取 upgrade-agent token 失败，改用 job token 评论：{e!r}')
        bot = token
    post_comment(api, number, body, bot)


if __name__ == '__main__':
    try:
        main()
    # 报告模式：任何失败只记日志，不让 job 变红挡住 automerge。
    except (Exception, SystemExit) as e:
        print(f'::warning::升级评审失败：{e!r}')
