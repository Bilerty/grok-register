import { useEffect, useMemo, useState } from "react";
import { RefreshCw, Search, ShieldAlert, Trash2 } from "lucide-react";
import { AccountPageContext } from "@/components/AccountPageContext";
import { Badge, Button, Card, EmptyState, Input, PageHeader, Toast } from "@/components/ui";
import { api, type FlaggedExitIp } from "@/lib/api";

export function FlaggedExitIpsPage() {
  const [items, setItems] = useState<FlaggedExitIp[]>([]);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [deleting, setDeleting] = useState("");
  const [toast, setToast] = useState<{ message: string; tone?: "default" | "success" | "error" }>({
    message: "",
  });

  const showToast = (message: string, tone: "default" | "success" | "error" = "default") => {
    setToast({ message, tone });
    window.setTimeout(() => setToast({ message: "" }), 2200);
  };

  const load = async () => {
    setLoading(true);
    try {
      const data = await api.flaggedExitIps();
      setItems(data.items || []);
    } catch (err: any) {
      showToast(err.message || "加载风控名单失败", "error");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const filtered = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    if (!keyword) return items;
    return items.filter((item) =>
      [item.ip, item.last_email, item.last_bot_flag_source, item.last_failure_reason]
        .join(" ")
        .toLowerCase()
        .includes(keyword)
    );
  }, [items, query]);

  const onDelete = async (ip: string) => {
    if (!window.confirm(`从风控名单中移除 ${ip}？下次注册遇到这个出口 IP 将不再自动换出口。`)) return;
    setDeleting(ip);
    try {
      await api.deleteFlaggedExitIp(ip);
      setItems((previous) => previous.filter((item) => item.ip !== ip));
      showToast(`已移除 ${ip}`, "success");
    } catch (err: any) {
      showToast(err.message || "删除失败", "error");
    } finally {
      setDeleting("");
    }
  };

  return (
    <div className="space-y-5 sm:space-y-6">
      <AccountPageContext crumbs={[{ label: "出口 IP 风控" }]} />
      <PageHeader
        title="出口 IP 风控名单"
        description="注册被判定风控时，会记下当时浏览器识别到的出口 IP。下次打开注册页前若仍是这些 IP，会重启浏览器换出口后再注册。"
        actions={
          <Button variant="outline" onClick={() => void load()} disabled={loading}>
            <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} aria-hidden="true" />
            刷新
          </Button>
        }
      />

      <Card className="overflow-hidden">
        <div className="border-b border-slate-200 p-4">
          <div className="relative">
            <Search className="pointer-events-none absolute left-3 top-3.5 h-4 w-4 text-slate-400" aria-hidden="true" />
            <Input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索出口 IP、邮箱或风控原因"
              className="pl-9"
            />
          </div>
          <div className="mt-2 text-xs text-muted-foreground">
            {loading ? "正在加载…" : `共 ${items.length} 个出口 IP${query.trim() ? `，当前显示 ${filtered.length} 个` : ""}`}
          </div>
        </div>

        {filtered.length ? (
          <>
            <div className="hidden overflow-x-auto md:block">
              <table className="w-full min-w-[760px] border-collapse text-left text-sm">
                <thead className="border-b border-slate-200 bg-slate-50/95 text-xs font-medium text-muted-foreground">
                  <tr>
                    <th className="px-4 py-2">出口 IP</th>
                    <th className="w-[88px] px-3 py-2">命中</th>
                    <th className="w-[140px] px-3 py-2">botFlagSource</th>
                    <th className="px-3 py-2">最近邮箱</th>
                    <th className="w-[168px] px-3 py-2">最近记录</th>
                    <th className="w-[72px] px-3 py-2 text-center">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((item) => (
                    <tr key={item.ip} className="align-top hover:bg-slate-50/70">
                      <td className="border-b border-slate-100 px-4 py-3">
                        <div className="flex items-start gap-2">
                          <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" aria-hidden="true" />
                          <div className="min-w-0">
                            <div className="break-all font-mono text-sm font-medium text-foreground">{item.ip}</div>
                            {item.last_failure_reason ? (
                              <div className="mt-1 break-words text-xs leading-5 text-muted-foreground">
                                {item.last_failure_reason}
                              </div>
                            ) : null}
                          </div>
                        </div>
                      </td>
                      <td className="border-b border-slate-100 px-3 py-3">
                        <Badge variant="warning">命中 {item.hit_count || 1}</Badge>
                      </td>
                      <td className="border-b border-slate-100 px-3 py-3 text-xs text-muted-foreground">
                        {item.last_bot_flag_source || "—"}
                      </td>
                      <td className="border-b border-slate-100 px-3 py-3">
                        <div className="break-all text-sm">{item.last_email || "—"}</div>
                      </td>
                      <td className="border-b border-slate-100 px-3 py-3 text-xs leading-5 text-muted-foreground">
                        <div>{item.last_seen_at || item.first_seen_at || "—"}</div>
                        {item.first_seen_at && item.last_seen_at && item.first_seen_at !== item.last_seen_at ? (
                          <div>首次 {item.first_seen_at}</div>
                        ) : null}
                      </td>
                      <td className="border-b border-slate-100 px-3 py-3 text-center">
                        <Button
                          size="icon"
                          variant="ghost"
                          className="h-9 w-9 text-red-700"
                          disabled={deleting === item.ip}
                          onClick={() => void onDelete(item.ip)}
                          aria-label={`移除 ${item.ip}`}
                        >
                          <Trash2 className="h-4 w-4" aria-hidden="true" />
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="divide-y divide-slate-100 md:hidden">
              {filtered.map((item) => (
                <article key={item.ip} className="space-y-3 p-4">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="break-all font-mono text-sm font-medium text-foreground">{item.ip}</div>
                      <div className="mt-1.5 flex flex-wrap gap-1.5">
                        <Badge variant="warning">命中 {item.hit_count || 1}</Badge>
                        {item.last_bot_flag_source ? (
                          <Badge variant="secondary">botFlagSource={item.last_bot_flag_source}</Badge>
                        ) : null}
                      </div>
                    </div>
                    <Button
                      size="icon"
                      variant="ghost"
                      className="h-9 w-9 text-red-700"
                      disabled={deleting === item.ip}
                      onClick={() => void onDelete(item.ip)}
                      aria-label={`移除 ${item.ip}`}
                    >
                      <Trash2 className="h-4 w-4" aria-hidden="true" />
                    </Button>
                  </div>
                  <div className="space-y-1 text-xs leading-5 text-muted-foreground">
                    {item.last_email ? <div className="break-all">最近邮箱 {item.last_email}</div> : null}
                    <div>
                      最近 {item.last_seen_at || item.first_seen_at || "—"}
                      {item.first_seen_at && item.last_seen_at && item.first_seen_at !== item.last_seen_at
                        ? ` · 首次 ${item.first_seen_at}`
                        : ""}
                    </div>
                    {item.last_failure_reason ? <div className="break-words">原因 {item.last_failure_reason}</div> : null}
                  </div>
                </article>
              ))}
            </div>
          </>
        ) : (
          <div className="p-4">
            <EmptyState
              title={loading ? "正在加载风控名单" : query.trim() ? "没有匹配的出口 IP" : "暂无风控出口 IP"}
              description={
                loading
                  ? "正在读取已记录的浏览器出口 IP。"
                  : query.trim()
                    ? "试试其他关键词，或清空搜索后再看全部名单。"
                    : "注册过程中出现风控后，对应的浏览器出口 IP 会显示在这里。"
              }
            />
          </div>
        )}
      </Card>

      <Toast message={toast.message} tone={toast.tone} />
    </div>
  );
}
