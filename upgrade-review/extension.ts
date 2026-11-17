// 升级评审的 pi 扩展：bash 在 OpenSandbox 的 Kata VM 里执行，submit_review 交结构化结论。
//
// 模型只有这两个工具，碰不到 runner：runner 上 pi 进程的环境里有网关 key，本地 read/bash 能读到
// /proc/<pid>/environ。sandbox 里只有仓库源码，出网由 sandbox 的 egress 策略挡住内网（review.py 创建时
// 设定），所以这里不再自己做 SSRF 检查。
import { createBashTool, type BashOperations, type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

// review.py 创建 sandbox 后经环境变量传入：server proxy 到 execd 的地址与 API key。
const EXECD = process.env.SANDBOX_EXECD_URL!;
const KEY = process.env.SANDBOX_API_KEY!;
const WORKDIR = "/workspace";

const sandboxOps: BashOperations = {
  async exec(command, _cwd, { onData, signal, timeout }) {
    const ms = (timeout ?? 300) * 1000;
    const res = await fetch(`${EXECD}/command`, {
      method: "POST",
      headers: { "OPEN-SANDBOX-API-KEY": KEY, "content-type": "application/json" },
      body: JSON.stringify({ command, cwd: WORKDIR, timeout: ms }),
      signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(ms + 30_000)]) : AbortSignal.timeout(ms + 30_000),
    });
    if (!res.ok || !res.body) throw new Error(`execd HTTP ${res.status}: ${await res.text()}`);
    // 每行一个 JSON 事件：stdout/stderr 的 text 是一行输出（不带换行），失败时 error.evalue 是退出码。
    let exitCode: number | null = 0;
    let buf = "";
    const handle = (line: string) => {
      if (!line.trim()) return;
      let e: { type?: string; text?: string; error?: { evalue?: string; traceback?: string[] } };
      try { e = JSON.parse(line.replace(/^data:\s*/, "")); } catch { return; }
      if (e.type === "stdout" || e.type === "stderr") onData(Buffer.from(`${e.text ?? ""}\n`));
      else if (e.type === "error") {
        const code = Number.parseInt(e.error?.evalue ?? "", 10);
        exitCode = Number.isNaN(code) ? null : code;
        if (exitCode === null) onData(Buffer.from(`${(e.error?.traceback ?? []).join("\n")}\n`));
      }
    };
    const decoder = new TextDecoder();
    for await (const chunk of res.body) {
      buf += decoder.decode(chunk, { stream: true });
      const lines = buf.split("\n");
      buf = lines.pop()!;
      lines.forEach(handle);
    }
    handle(buf);
    return { exitCode };
  },
};

export default function (pi: ExtensionAPI) {
  const bash = createBashTool(WORKDIR, { operations: sandboxOps });
  pi.registerTool({
    ...bash,
    description:
      `${bash.description}\n\n命令在一次性 Linux VM 里执行，工作目录 ${WORKDIR} 是 PR head 的仓库源码（没有 .git）。` +
      "可以联网访问公网（curl、git clone、包管理器），内网地址不通。可以随意安装工具、运行构建或测试，VM 用完即删。",
  });

  pi.registerTool({
    name: "submit_review",
    label: "Submit review",
    description: "提交最终评审结论。评审结束时必须调用且只调用一次；调用后即结束，不再输出其他内容。",
    parameters: Type.Object({
      verdict: Type.Union([Type.Literal("safe"), Type.Literal("needs_review"), Type.Literal("unsafe")]),
      summary: Type.String({ description: "一两句话：升级了什么、结论的主要依据" }),
      breaking_changes: Type.Array(Type.String(), { description: "与本仓库用法相关的上游破坏性变更；无则为空" }),
      risks: Type.Array(Type.String(), { description: "其他风险或不确定之处" }),
      human_checks: Type.Array(Type.String(), { description: "合并前建议人工确认的具体事项" }),
      sources: Type.Array(Type.String(), { description: "实际查阅过的 URL 与仓库文件路径" }),
    }),
    async execute() {
      // 结论由 review.py 从 tool_execution_start 事件的参数里取，这里只确认收到。
      return { content: [{ type: "text", text: "已提交。" }], details: {} };
    },
  });
}
