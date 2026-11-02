#!/usr/bin/env python3
"""在当前 PR 上新建或更新唯一一条评论。

  pr_comment.py <markdown-file> <name>

name 写进评论开头的 HTML 注释，用来找回同一条评论，重跑时原地更新而不是刷屏。
需要 GITEA_TOKEN 和 pull_request 事件。
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

# Gitea 评论有长度上限；超出截断，完整输出看 job 日志。
MAX_COMMENT = 60000


def request(url, method='GET', body=None, headers=None):
    req = urllib.request.Request(
        url, method=method, headers={'Content-Type': 'application/json', **(headers or {})},
        data=json.dumps(body).encode() if body is not None else None,
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response) if response.status != 204 else None


def comment(markdown_file, name):
    marker = f'<!-- {name} -->'
    body = marker + '\n' + Path(markdown_file).read_text()
    if len(body) > MAX_COMMENT:
        body = body[:MAX_COMMENT] + '\n```\n\n…已截断，完整输出见 job 日志。'
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
    api = f"{os.environ['GITHUB_SERVER_URL']}/api/v1/repos/{os.environ['GITHUB_REPOSITORY']}"
    auth = {'Authorization': f"token {os.environ['GITEA_TOKEN']}"}
    issue = f"{api}/issues/{event['pull_request']['number']}/comments"
    # ponytail: 只看第一页评论（默认 50 条），评论更多的 PR 会新建而非更新
    mine = [c for c in request(issue, headers=auth) if c['body'].startswith(marker)]
    if mine:
        request(f"{api}/issues/comments/{mine[0]['id']}", 'PATCH', {'body': body}, auth)
    else:
        request(issue, 'POST', {'body': body}, auth)


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    comment(*sys.argv[1:])
