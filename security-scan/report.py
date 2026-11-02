#!/usr/bin/env python3
"""把各扫描器的 JSON 结果汇总成 markdown（PR 评论与 job 日志共用）。

  report.py <结果目录> <范围说明> <启用的扫描器，逗号分隔>

只输出规则、位置和版本号，从不输出代码行：semgrep 的命中行、gitleaks 的 Match
可能就是密钥本身，而评论和日志的可见范围都比 secret 应有的大。
启用了却缺少 <scanner>.json 表示该扫描器运行失败。
"""
import json
import sys
from pathlib import Path


def semgrep(d):
    rows = [f"| {r['extra']['severity']} | `{r['check_id']}` | `{r['path']}:{r['start']['line']}` |"
            for r in d['results']]
    return rows, '| 级别 | 规则 | 位置 |\n|---|---|---|'


def gitleaks(d):
    # Fingerprint 是 commit:file:rule:line，不含密钥；确认已轮换后原样写进 .gitleaksignore。
    rows = [f"| `{r['RuleID']}` | `{r['File']}:{r['StartLine']}` | `{r['Fingerprint']}` |" for r in d or []]
    return rows, '| 规则 | 位置 | Fingerprint |\n|---|---|---|'


def trivy(d):
    rows = [f"| {v['Severity']} | {v['VulnerabilityID']} | `{v['PkgName']}` {v['InstalledVersion']}"
            f" → {v.get('FixedVersion') or '无修复'} | `{r['Target']}` |"
            for r in d.get('Results') or [] for v in r.get('Vulnerabilities') or []]
    return rows, '| 级别 | 漏洞 | 包 | 来源 |\n|---|---|---|---|'


SCANNERS = {'semgrep': ('代码', semgrep), 'gitleaks': ('密钥', gitleaks), 'trivy': ('依赖漏洞，全量', trivy)}


def report(out, scope, enabled):
    md = [f'## 安全扫描（报告模式，不阻塞合并）\n\n范围：{scope}\n']
    for name, (what, parse) in SCANNERS.items():
        if name not in enabled:
            continue
        f = Path(out, f'{name}.json')
        if not f.exists():
            md.append(f'### {name}（{what}）— ❌ 运行失败，见 job 日志\n')
            continue
        rows, header = parse(json.loads(f.read_text() or 'null'))
        md.append(f'### {name}（{what}）— ' + (f'⚠ {len(rows)} 项' if rows else '✅ 无发现') + '\n')
        if rows:
            md.append(f'<details><summary>明细</summary>\n\n{header}\n' + '\n'.join(rows) + '\n</details>\n')
    return '\n'.join(md)


if __name__ == '__main__':
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    print(report(sys.argv[1], sys.argv[2], sys.argv[3].split(',')))
