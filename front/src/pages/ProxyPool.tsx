import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, Network, RefreshCw, RotateCcw, Trash2, Upload } from "lucide-react";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  Input,
  Label,
  PageHeader,
  Select,
  StatCard,
  Switch,
  Textarea,
  Toast,
} from "@/components/ui";
import { api, type ProxyPoolSummary } from "@/lib/api";

const STATUS_META: Record<string, { label: string; variant: "success" | "warning" | "destructive" | "secondary" }> = {
  healthy: { label: "健康", variant: "success" },
  unreachable: { label: "不可达", variant: "destructive" },
  cooldown: { label: "冷却中", variant: "warning" },
  flagged: { label: "IP 被风控", variant: "secondary" },
};

const MODE_OPTIONS = [
  { value: "", label: "自动识别（推荐）" },
  { value: "static", label: "static：单代理" },
  { value: "pool", label: "pool：代理池" },
  { value: "sticky_template", label: "sticky_template：粘性模板" },
];

const SELECTION_OPTIONS = [
  { value: "round_robin", label: "轮询 round_robin" },
  { value: "random", label: "随机 random" },
  { value: "least_used", label: "最少使用 least_used" },
];

const SCOPE_OPTIONS = [
  { value: "task", label: "任务粘性（worker 绑定节点）" },
  { value: "none", label: "不粘性（每次选择可能变化）" },
];

const PROXY_CONFIG_KEYS = [
  "proxy_mode",
  "proxy_selection",
  "proxy_sticky_scope",
  "proxy_file",
  "proxy_username",
  "proxy_password",
  "proxy_cooldown_seconds",
  "proxy_probe_once_per_batch",
] as const;

function formatTime(epoch: number): string {
  if (!epoch) return "—";
  return new Date(epoch * 1000).toLocaleString();
}

export function ProxyPoolPage() {
  const [pool, setPool] = useState<ProxyPoolSummary | null>(null);
  const [config, setConfig] = useState<Record<string, any>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [probing, setProbing] = useState(false);
  const [busyKey, setBusyKey] = useState("");
  const [importOpen, setImportOpen] = useState(false);
  const [importText, setImportText] = useState("");
  const [importing, setImporting] = useState(false);
  const [toast, setToast] = useState<{ message: string; tone?: "default" | "success" | "error" }>({
    message: "",
  });

  const showToast = (message: string, tone: "default" | "success" | "error" = "default") => {
    setToast({ message, tone });
    window.setTimeout(() => setToast({ message: "" }), 2400);
  };

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [poolData, configData] = await Promise.all([api.proxyPool(), api.getConfig()]);
      setPool(poolData);
      setConfig(configData.config || {});
    } catch (err: any) {
      showToast(err.message || "加载代理池失败", "error");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const setField = (key: string, value: any) => setConfig((prev) => ({ ...prev, [key]: value }));

  const onSave = async () => {
    setSaving(true);
    try {
      const patch: Record<string, any> = {};
      for (const key of PROXY_CONFIG_KEYS) {
        if (key in config) patch[key] = config[key];
      }
      await api.saveConfig(patch);
      await load();
      showToast("代理池配置已保存", "success");
    } catch (err: any) {
      showToast(err.message || "保存失败", "error");
    } finally {
      setSaving(false);
    }
  };

  const onProbeAll = async () => {
    setProbing(true);
    try {
      const stats = await api.probeProxyPool();
      showToast(`探测完成：健康 ${stats.healthy}/${stats.total}`, stats.unreachable ? "default" : "success");
      await load();
    } catch (err: any) {
      showToast(err.message || "探测失败", "error");
    } finally {
      setProbing(false);
    }
  };

  const onImport = async () => {
    const lines = importText
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    if (!lines.length) {
      showToast("请粘贴至少一条代理", "error");
      return;
    }
    setImporting(true);
    try {
      const result = await api.importProxyPool(lines);
      const invalidNote = result.invalid?.length ? `，无效 ${result.invalid.length} 条` : "";
      showToast(`已导入 ${result.added?.length || 0} 个节点${invalidNote}`, "success");
      setImportText("");
      setImportOpen(false);
      await load();
    } catch (err: any) {
      showToast(err.message || "导入失败", "error");
    } finally {
      setImporting(false);
    }
  };

  const onNodeAction = async (key: string, action: "probe" | "reset" | "remove") => {
    setBusyKey(`${action}:${key}`);
    try {
      if (action === "probe") {
        const { result } = await api.probeProxyPoolNode(key);
        showToast(result.ok ? `可用，出口 IP ${result.egress_ip || "未知"}` : result.error || "探测失败", result.ok ? "success" : "error");
      } else if (action === "reset") {
        await api.clearProxyPoolCooldown(key);
        showToast("节点已复位", "success");
      } else {
        if (!window.confirm("从代理池移除该节点？")) return;
        await api.removeProxyPoolNode(key);
        showToast("节点已移除", "success");
      }
      await load();
    } catch (err: any) {
      showToast(err.message || "操作失败", "error");
    } finally {
      setBusyKey("");
    }
  };

  const onClearAll = async () => {
    if (!window.confirm("清空代理池全部节点？该操作会同时清空配置中的池文本。")) return;
    try {
      const result = await api.clearProxyPool();
      showToast(`已清空 ${result.removed} 个节点`, "success");
      await load();
    } catch (err: any) {
      showToast(err.message || "清空失败", "error");
    }
  };

  const nodes = pool?.nodes || [];
  const statusCount = (status: string) => nodes.filter((node) => node.status === status).length;
  const isPoolMode = pool?.mode === "pool";

  return (
    <div className="space-y-5 sm:space-y-6">
      <PageHeader
        title="代理池"
        description="注册浏览器与 xAI/OAuth 请求共用任务绑定的池出口；节点健康自动管理，出口 IP 命中风控名单会自动隔离。"
        actions={
          <>
            <Button variant="outline" onClick={() => void load()} disabled={loading}>
              <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} aria-hidden="true" />
              刷新
            </Button>
            <Button variant="outline" onClick={() => void onProbeAll()} disabled={probing || !isPoolMode}>
              <RefreshCw className={`h-4 w-4 ${probing ? "animate-spin" : ""}`} aria-hidden="true" />
              全部探测
            </Button>
            <Button variant="outline" onClick={() => setImportOpen((prev) => !prev)}>
              <Upload className="h-4 w-4" aria-hidden="true" />
              导入节点
            </Button>
            <Button variant="ghost" className="text-red-700" onClick={() => void onClearAll()} disabled={!isPoolMode}>
              <Trash2 className="h-4 w-4" aria-hidden="true" />
              清空
            </Button>
          </>
        }
      />

      {pool?.error ? (
        <div className="flex items-start gap-2 rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-800">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
          <span>代理池配置有误，已回退为不启用：{pool.error}</span>
        </div>
      ) : null}

      {importOpen ? (
        <Card className="p-4">
          <Label htmlFor="proxy-pool-import">每行一条 HTTP(S) 代理，支持 http://user:pass@host:port</Label>
          <Textarea
            id="proxy-pool-import"
            className="mt-2 min-h-32 font-mono text-xs"
            placeholder={"hk-office-01 | http://user:pass@gw.vendor.com:4000\nhttp://127.0.0.1:7890"}
            value={importText}
            onChange={(event) => setImportText(event.target.value)}
          />
          <p className="mt-2 text-xs leading-5 text-muted-foreground">
            支持 `名称 | 代理URL`（自动去除前后空格）或纯 URL；无名称条目按本批次哈希+顺序编号自动命名。
          </p>
          <div className="mt-3 flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setImportOpen(false)}>
              取消
            </Button>
            <Button onClick={() => void onImport()} disabled={importing}>
              确认导入
            </Button>
          </div>
        </Card>
      ) : null}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard title="节点总数" value={pool?.count ?? "—"} hint={`模式：${pool?.mode || "—"}`} icon={<Network className="h-4 w-4" />} />
        <StatCard title="健康" value={pool?.healthy ?? "—"} accent="success" />
        <StatCard title="冷却中" value={statusCount("cooldown")} accent="warning" hint={`${pool?.cooldown_seconds ?? 0}s / 次`} />
        <StatCard title="不可达 / 被风控" value={`${statusCount("unreachable")} / ${statusCount("flagged")}`} accent="destructive" />
      </div>

      <Card className="p-4 sm:p-5">
        <div className="mb-4 flex items-center justify-between">
          <div>
            <h2 className="text-base font-semibold text-foreground">池配置</h2>
            <p className="mt-1 text-xs leading-5 text-muted-foreground">
              全局代理本身在「注册设置 → 浏览器」中填写；本页只管理池行为。
            </p>
          </div>
          <Button onClick={() => void onSave()} disabled={saving}>
            {saving ? "保存中…" : "保存配置"}
          </Button>
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="min-w-0 space-y-2">
            <Label htmlFor="proxy_mode">池模式</Label>
            <Select id="proxy_mode" value={config.proxy_mode ?? ""} onChange={(event) => setField("proxy_mode", event.target.value)}>
              {MODE_OPTIONS.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </Select>
          </div>
          <div className="min-w-0 space-y-2">
            <Label htmlFor="proxy_selection">选择策略</Label>
            <Select
              id="proxy_selection"
              value={config.proxy_selection ?? "round_robin"}
              onChange={(event) => setField("proxy_selection", event.target.value)}
            >
              {SELECTION_OPTIONS.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </Select>
          </div>
          <div className="min-w-0 space-y-2">
            <Label htmlFor="proxy_sticky_scope">粘性范围</Label>
            <Select
              id="proxy_sticky_scope"
              value={config.proxy_sticky_scope ?? "task"}
              onChange={(event) => setField("proxy_sticky_scope", event.target.value)}
            >
              {SCOPE_OPTIONS.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </Select>
            <p className="text-xs leading-5 text-muted-foreground">
              每个注册 worker 绑定一个健康节点，浏览器与 HTTP 同出口；节点失效自动切换。
            </p>
          </div>
          <div className="min-w-0 space-y-2">
            <Label htmlFor="proxy_file">池文件路径（可选）</Label>
            <Input
              id="proxy_file"
              value={config.proxy_file ?? ""}
              onChange={(event) => setField("proxy_file", event.target.value)}
              placeholder="/app/data/proxies.txt"
            />
            <p className="text-xs leading-5 text-muted-foreground">文件内每行一条代理，会与「网络代理」多行内容合并。</p>
          </div>
          <div className="min-w-0 space-y-2">
            <Label htmlFor="proxy_username">池级用户名（可选）</Label>
            <Input
              id="proxy_username"
              value={config.proxy_username ?? ""}
              onChange={(event) => setField("proxy_username", event.target.value)}
              autoComplete="off"
            />
          </div>
          <div className="min-w-0 space-y-2">
            <Label htmlFor="proxy_password">池级密码（可选）</Label>
            <Input
              id="proxy_password"
              type="password"
              value={config.proxy_password ?? ""}
              onChange={(event) => setField("proxy_password", event.target.value)}
              autoComplete="new-password"
            />
          </div>
          <div className="min-w-0 space-y-2">
            <Label htmlFor="proxy_cooldown_seconds">风控冷却秒数</Label>
            <Input
              id="proxy_cooldown_seconds"
              type="number"
              min={0}
              value={config.proxy_cooldown_seconds ?? 600}
              onChange={(event) => setField("proxy_cooldown_seconds", Number(event.target.value))}
            />
          </div>
          <div className="min-w-0 space-y-2">
            <Label>批次统一预探测</Label>
            <div className="flex h-10 items-center">
              <Switch
                checked={Boolean(config.proxy_probe_once_per_batch ?? true)}
                onCheckedChange={(checked: boolean) => setField("proxy_probe_once_per_batch", checked)}
                label="批次开始前统一预探测池节点"
              />
            </div>
            <p className="text-xs leading-5 text-muted-foreground">
              开启后批次开始时统一探测全部节点，注册过程中不再逐节点探测。
            </p>
          </div>
        </div>
      </Card>

      <Card className="overflow-hidden">
        {nodes.length ? (
          <>
            <div className="hidden overflow-x-auto md:block">
              <table className="w-full min-w-[1080px] border-collapse text-left text-sm">
                <thead className="border-b border-slate-200 bg-slate-50/95 text-xs font-medium text-muted-foreground">
                  <tr>
                    <th className="w-[150px] px-3 py-2">名称</th>
                    <th className="px-4 py-2">节点</th>
                    <th className="w-[100px] px-3 py-2">状态</th>
                    <th className="w-[130px] px-3 py-2">出口 IP</th>
                    <th className="w-[150px] px-3 py-2">ASN</th>
                    <th className="w-[80px] px-3 py-2">延迟</th>
                    <th className="w-[160px] px-3 py-2">最近使用</th>
                    <th className="w-[96px] px-3 py-2 text-center">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {nodes.map((node) => {
                    const meta = STATUS_META[node.status] || STATUS_META.healthy;
                    return (
                      <tr key={node.key} className="align-top hover:bg-slate-50/70">
                        <td className="border-b border-slate-100 px-3 py-3 text-sm font-medium text-foreground">
                          <div className="break-all">{node.name || "—"}</div>
                        </td>
                        <td className="border-b border-slate-100 px-4 py-3">
                          <div className="break-all font-mono text-xs text-foreground">{node.url || "（空）"}</div>
                          {node.last_error ? (
                            <div className="mt-1 break-words text-xs leading-5 text-muted-foreground">{node.last_error}</div>
                          ) : null}
                        </td>
                        <td className="border-b border-slate-100 px-3 py-3">
                          <Badge variant={meta.variant}>{meta.label}</Badge>
                          {node.status === "cooldown" && node.cooldown_remaining > 0 ? (
                            <div className="mt-1 text-xs text-muted-foreground">剩 {node.cooldown_remaining}s</div>
                          ) : null}
                        </td>
                        <td className="border-b border-slate-100 px-3 py-3 font-mono text-xs">{node.egress_ip || "—"}</td>
                        <td className="border-b border-slate-100 px-3 py-3 font-mono text-xs" title={node.asn || ""}>
                          {node.asn || <span className="text-muted-foreground">—</span>}
                        </td>
                        <td className="border-b border-slate-100 px-3 py-3 text-xs">
                          {node.latency_ms != null ? `${node.latency_ms} ms` : "—"}
                        </td>
                        <td className="border-b border-slate-100 px-3 py-3 text-xs text-muted-foreground">
                          {formatTime(node.last_used_at)}
                        </td>
                        <td className="border-b border-slate-100 px-3 py-3">
                          <div className="flex items-center justify-center gap-1">
                            <Button
                              size="icon"
                              variant="ghost"
                              className="h-8 w-8"
                              disabled={busyKey === `probe:${node.key}`}
                              onClick={() => void onNodeAction(node.key, "probe")}
                              aria-label="探测该节点"
                            >
                              <RefreshCw className="h-4 w-4" aria-hidden="true" />
                            </Button>
                            <Button
                              size="icon"
                              variant="ghost"
                              className="h-8 w-8"
                              disabled={busyKey === `reset:${node.key}` || node.status === "healthy"}
                              onClick={() => void onNodeAction(node.key, "reset")}
                              aria-label="复位该节点"
                            >
                              <RotateCcw className="h-4 w-4" aria-hidden="true" />
                            </Button>
                            <Button
                              size="icon"
                              variant="ghost"
                              className="h-8 w-8 text-red-700"
                              disabled={busyKey === `remove:${node.key}`}
                              onClick={() => void onNodeAction(node.key, "remove")}
                              aria-label="移除该节点"
                            >
                              <Trash2 className="h-4 w-4" aria-hidden="true" />
                            </Button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            <div className="divide-y divide-slate-100 md:hidden">
              {nodes.map((node) => {
                const meta = STATUS_META[node.status] || STATUS_META.healthy;
                return (
                  <article key={node.key} className="space-y-2 p-4">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="break-all text-sm font-medium text-foreground">{node.name || "（未命名）"}</div>
                        <div className="mt-0.5 break-all font-mono text-xs text-muted-foreground">{node.url || "（空）"}</div>
                      </div>
                      <Badge variant={meta.variant}>{meta.label}</Badge>
                    </div>
                    <div className="text-xs leading-5 text-muted-foreground">
                      <div>出口 IP {node.egress_ip || "—"}</div>
                      <div>ASN {node.asn || "—"}</div>
                      <div>
                        延迟 {node.latency_ms != null ? `${node.latency_ms} ms` : "—"} · 最近 {formatTime(node.last_used_at)}
                      </div>
                      {node.last_error ? <div className="break-words">{node.last_error}</div> : null}
                    </div>
                    <div className="flex gap-2">
                      <Button size="sm" variant="outline" onClick={() => void onNodeAction(node.key, "probe")}>
                        探测
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={node.status === "healthy"}
                        onClick={() => void onNodeAction(node.key, "reset")}
                      >
                        复位
                      </Button>
                      <Button size="sm" variant="ghost" className="text-red-700" onClick={() => void onNodeAction(node.key, "remove")}>
                        移除
                      </Button>
                    </div>
                  </article>
                );
              })}
            </div>
          </>
        ) : (
          <div className="p-4">
            <EmptyState
              title={loading ? "正在加载代理池" : "暂无池节点"}
              description={
                loading
                  ? "正在读取池状态。"
                  : isPoolMode
                    ? "点击右上角「导入节点」，粘贴每行一条的 HTTP(S) 代理。"
                    : "导入首个节点后会自动切换为 pool 模式；单代理场景无需使用代理池。"
              }
            />
          </div>
        )}
      </Card>

      <Toast message={toast.message} tone={toast.tone} />
    </div>
  );
}
