import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type ProxyPoolNode, type ProxyPoolResponse } from "@/lib/api";
import { Button } from "@/components/ui";
import { RefreshCw, RotateCw, Snowflake, Trash2, Upload, XCircle, CheckCircle2 } from "lucide-react";

type PoolInfo = ProxyPoolResponse["pool"] | null;

function fmtTime(value: number | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value * 1000);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function StatusBadge({ node }: { node: ProxyPoolNode }) {
  if (node.status === "healthy") {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-emerald-50 px-2 py-0.5 text-xs font-medium text-emerald-700">
        <CheckCircle2 className="h-3.5 w-3.5" /> 健康
      </span>
    );
  }
  if (node.status === "unreachable") {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-red-50 px-2 py-0.5 text-xs font-medium text-red-700">
        <XCircle className="h-3.5 w-3.5" /> 不可达
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-amber-50 px-2 py-0.5 text-xs font-medium text-amber-700">
      <Snowflake className="h-3.5 w-3.5" /> 冷却 {Math.max(1, Math.ceil(node.cooldown_remaining / 60))} 分钟
    </span>
  );
}

export function ProxyPoolPage() {
  const [data, setData] = useState<ProxyPoolResponse | null>(null);
  const [page, setPage] = useState(1);
  const [importText, setImportText] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const pageSize = 20;

  const load = useCallback(async (targetPage = page) => {
    try {
      const result = await api.proxyPool(targetPage, pageSize);
      setData(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [page, pageSize]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 15000);
    return () => window.clearInterval(timer);
  }, [load]);

  const pool = (data?.pool ?? null) as PoolInfo;
  const totalPages = data ? Math.max(1, Math.ceil(data.total / pageSize)) : 1;

  const run = async (action: () => Promise<unknown>, successMessage: string) => {
    setBusy(true);
    setMessage("");
    setError("");
    try {
      await action();
      setMessage(successMessage);
      await load(page);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const handleImport = () => run(
    () => api.proxyPoolImport(importText).then(() => setImportText("")),
    "导入完成"
  );
  const handleProbeAll = () => run(() => api.proxyPoolProbe(), "探测完成");
  const handleProbeNode = (url: string) => run(() => api.proxyPoolProbeNode(url), "节点探测完成");
  const handleClear = (url: string) => run(() => api.proxyPoolClearCooldown(url), "已解除冷却");
  const handleRemove = (url: string) => {
    if (!window.confirm("确认移除该代理节点？")) return;
    void run(() => api.proxyPoolRemoveNode(url), "已移除");
  };

  const summary = useMemo(() => {
    if (!data) return null;
    const items = data.items;
    const healthy = items.filter((item) => item.status === "healthy").length;
    const unreachable = items.filter((item) => item.status === "unreachable").length;
    const cooldown = items.filter((item) => item.status === "cooldown").length;
    return { healthy, unreachable, cooldown };
  }, [data]);

  return (
    <div className="mx-auto max-w-6xl space-y-6 p-4 sm:p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-slate-900">代理池管理</h1>
          <p className="mt-1 text-sm text-slate-500">
            批量导入 HTTP(S) 代理，查看出口 IP、延迟与健康状态；不可达或冷却中的节点不会被调度。
          </p>
        </div>
        <Button variant="outline" size="sm" disabled={busy} onClick={() => void handleProbeAll()}>
          <RefreshCw className="mr-1 h-4 w-4" /> 探测全部节点
        </Button>
      </div>

      {pool ? (
        <div className="flex flex-wrap gap-3 text-sm">
          <span className="rounded-lg bg-slate-100 px-3 py-1.5 text-slate-700">
            模式 <b>{pool.mode}</b> · 选择器 <b>{pool.selection}</b> · 冷却 <b>{pool.cooldown_seconds}s</b>
          </span>
          <span className="rounded-lg bg-slate-100 px-3 py-1.5 text-slate-700">
            共 <b>{pool.count}</b> 节点
          </span>
          {pool.healthy !== null ? (
            <span className="rounded-lg bg-emerald-50 px-3 py-1.5 text-emerald-700">
              健康 <b>{pool.healthy}</b>（本页 {summary?.healthy} / 冷却 {summary?.cooldown} / 不可达 {summary?.unreachable}）
            </span>
          ) : null}
        </div>
      ) : null}

      {message ? <p className="rounded-lg bg-emerald-50 px-3 py-2 text-sm text-emerald-700">{message}</p> : null}
      {error ? <p className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p> : null}

      <div className="rounded-xl border bg-white p-4">
        <h2 className="text-sm font-medium text-slate-900">批量导入</h2>
        <p className="mt-1 text-xs text-slate-500">每行一个 http(s):// 代理地址，自动校验并去重；导入后写入代理配置。</p>
        <textarea
          className="mt-3 h-32 w-full rounded-lg border border-slate-200 p-3 font-mono text-xs focus:border-sky-400 focus:outline-none"
          placeholder={"http://user:pass@1.2.3.4:8080\nhttp://user:pass@5.6.7.8:8080"}
          value={importText}
          onChange={(event) => setImportText(event.target.value)}
        />
        <div className="mt-3 flex justify-end">
          <Button size="sm" disabled={busy || !importText.trim()} onClick={() => void handleImport()}>
            <Upload className="mr-1 h-4 w-4" /> 导入
          </Button>
        </div>
      </div>

      <div className="overflow-x-auto rounded-xl border bg-white">
        <table className="min-w-full text-sm">
          <thead>
            <tr className="border-b bg-slate-50 text-left text-xs text-slate-500">
              <th className="px-3 py-2.5 font-medium">代理地址（密码已隐去）</th>
              <th className="px-3 py-2.5 font-medium">出口 IP</th>
              <th className="px-3 py-2.5 font-medium">延迟</th>
              <th className="px-3 py-2.5 font-medium">上次使用</th>
              <th className="px-3 py-2.5 font-medium">状态</th>
              <th className="px-3 py-2.5 text-right font-medium">操作</th>
            </tr>
          </thead>
          <tbody>
            {(data?.items ?? []).map((node) => (
              <tr key={node.url} className="border-b last:border-b-0 hover:bg-slate-50/60">
                <td className="max-w-xs truncate px-3 py-2.5 font-mono text-xs text-slate-700" title={node.url_display}>
                  {node.url_display}
                </td>
                <td className="px-3 py-2.5 font-mono text-xs text-slate-700">{node.egress_ip || "—"}</td>
                <td className="px-3 py-2.5 text-xs text-slate-700">
                  {node.latency_ms !== null && node.latency_ms !== undefined ? `${node.latency_ms} ms` : "—"}
                </td>
                <td className="px-3 py-2.5 text-xs text-slate-500">{fmtTime(node.last_used_at)}</td>
                <td className="px-3 py-2.5"><StatusBadge node={node} /></td>
                <td className="px-3 py-2.5">
                  <div className="flex items-center justify-end gap-1">
                    <Button variant="ghost" size="sm" disabled={busy} title="探测该节点" onClick={() => void handleProbeNode(node.url)}>
                      <RotateCw className="h-4 w-4" />
                    </Button>
                    {node.status === "cooldown" ? (
                      <Button variant="ghost" size="sm" disabled={busy} title="解除冷却" onClick={() => void handleClear(node.url)}>
                        <Snowflake className="h-4 w-4" />
                      </Button>
                    ) : null}
                    <Button variant="ghost" size="sm" disabled={busy} title="移除" onClick={() => handleRemove(node.url)}>
                      <Trash2 className="h-4 w-4 text-red-500" />
                    </Button>
                  </div>
                </td>
              </tr>
            ))}
            {(data?.items ?? []).length === 0 ? (
              <tr>
                <td colSpan={6} className="px-3 py-10 text-center text-sm text-slate-400">
                  暂无代理节点；请先在「注册设置」中配置代理池（proxy_mode=pool），或在上方批量导入。
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>

      {totalPages > 1 ? (
        <div className="flex items-center justify-between text-sm text-slate-600">
          <Button variant="outline" size="sm" disabled={page <= 1 || busy} onClick={() => setPage((value) => value - 1)}>
            上一页
          </Button>
          <span>
            第 {page} / {totalPages} 页（共 {data?.total ?? 0} 节点）
          </span>
          <Button variant="outline" size="sm" disabled={page >= totalPages || busy} onClick={() => setPage((value) => value + 1)}>
            下一页
          </Button>
        </div>
      ) : null}
    </div>
  );
}
