import { useEffect, useState } from "react";
import { Archive, Copy, Download, Loader2, Search, UploadCloud } from "lucide-react";
import { AccountEmailLabel, EmailProviderIcon, EmailProviderLabel } from "@/components/AccountEmailIcon";
import { AccountPageContext } from "@/components/AccountPageContext";
import { AccountFilterBar, AccountSelectionToolbar } from "@/components/AccountTableToolbar";
import { Badge, Button, Card, EmptyState, Input, PageHeader, PaginationBar, Select, Toast } from "@/components/ui";
import { api, type AccountRecord, type AuthKind } from "@/lib/api";
import { copyText } from "@/lib/utils";

export function CredentialsPage() {
  const [items, setItems] = useState<AccountRecord[]>([]);
  const [selected, setSelected] = useState<Record<number, boolean>>({});
  const [query, setQuery] = useState("");
  const [tab, setTab] = useState<AuthKind>("cpa");
  const [loading, setLoading] = useState(true);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [total, setTotal] = useState(0);
  const [selectingAll, setSelectingAll] = useState(false);
  const [busy, setBusy] = useState("");
  const [toast, setToast] = useState<{ message: string; tone?: "default" | "success" | "error" }>({ message: "" });

  const notify = (message: string, tone: "default" | "success" | "error" = "default") => {
    setToast({ message, tone });
    window.setTimeout(() => setToast({ message: "" }), 2400);
  };

  const load = async (targetPage = page, targetQuery = query, targetPageSize = pageSize) => {
    setLoading(true);
    try {
      const result = await api.accounts({ limit: targetPageSize, offset: (targetPage - 1) * targetPageSize, q: targetQuery.trim() });
      setItems(result.items || []);
      setTotal(Number(result.total ?? result.items?.length ?? 0));
      setPage(targetPage);
    } catch (error: any) {
      notify(error.message || "授权文件加载失败", "error");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setSelected({});
      void load(1, query);
    }, 280);
    return () => window.clearTimeout(timer);
  }, [query]);

  const filtered = items;
  const isAvailable = (item: AccountRecord) => tab === "cpa"
    ? item.cpa_auth_available
    : tab === "grok2api"
      ? item.grok2api_auth_available
      : item.sso_available;
  const selectedIds = Object.entries(selected).filter(([, value]) => value).map(([id]) => Number(id));
  const availableCount = filtered.filter(isAvailable).length;
  const selectableItems = filtered.filter(isAvailable);
  const allVisibleSelected = selectableItems.length > 0 && selectableItems.every((item) => selected[item.id]);

  const toggleAllVisible = (checked: boolean) => {
    setSelected((previous) => {
      const next = { ...previous };
      for (const item of selectableItems) {
        if (checked) next[item.id] = true;
        else delete next[item.id];
      }
      return next;
    });
  };

  const selectAllAvailable = async () => {
    setSelectingAll(true);
    try {
      const result = await api.actionableAccountIds("auth_export", query.trim(), "", tab);
      setSelected(Object.fromEntries((result.ids || []).map((id) => [id, true])));
      notify(`已选择 ${result.total} 个可用文件`, "success");
    } catch (error: any) {
      notify(error.message || "选择全部文件失败", "error");
    } finally {
      setSelectingAll(false);
    }
  };

  const batchDownload = async () => {
    if (!selectedIds.length) return;
    setBusy("download");
    try {
      const result = await api.downloadAuthArchive(selectedIds, tab);
      const url = URL.createObjectURL(result.blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = result.filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      notify(`已导出 ${result.exported} 个文件${result.skipped ? `，跳过 ${result.skipped} 个` : ""}`, "success");
    } catch (error: any) {
      notify(error.message || "批量导出失败", "error");
    } finally {
      setBusy("");
    }
  };

  const copyJson = async (item: AccountRecord) => {
    setBusy(`copy-${item.id}`);
    try {
      const result = await api.accountAuthJson(item.id, tab);
      notify((await copyText(result.content)) ? (tab === "sso" ? "SSO 已复制" : "JSON 已复制") : "复制失败", "success");
    } catch (error: any) {
      notify(error.message || "读取 JSON 失败", "error");
    } finally {
      setBusy("");
    }
  };

  const importGrok2API = async (item: AccountRecord) => {
    setBusy(`import-${item.id}`);
    try {
      const result = await api.importAccountToGrok2API(item.id);
      setItems((old) => old.map((row) => row.id === item.id ? result.item : row));
      notify("已导入 Grok2API", "success");
    } catch (error: any) {
      notify(error.message || "导入失败", "error");
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="space-y-5">
      <AccountPageContext crumbs={[{ label: "授权文件" }]} />
      <PageHeader
        title="授权文件"
        description="集中查看、复制和导出 CPA 与 Grok2API 授权文件，减少账号列表中的操作负担。"
      />

      <section className="grid gap-3 sm:grid-cols-3">
        {[
          ["账号记录", total],
          [tab === "cpa" ? "本页 CPA 文件" : tab === "grok2api" ? "本页 Grok2API 文件" : "本页 SSO", availableCount],
          ["已选择", selectedIds.length],
        ].map(([label, value]) => (
          <Card key={String(label)} className="p-4">
            <div className="text-xs text-slate-500">{label}</div>
            <div className="mt-2 text-2xl font-semibold tabular-nums text-slate-950">{value}</div>
          </Card>
        ))}
      </section>

      <Card className="overflow-hidden">
        <AccountFilterBar>
            <div className="w-full sm:w-56">
              <label htmlFor="credential-kind" className="mb-1.5 block text-xs font-medium text-slate-500">文件类型</label>
              <Select
                id="credential-kind"
                value={tab}
                onChange={(event) => { setTab(event.target.value as AuthKind); setSelected({}); }}
              >
                <option value="cpa">CPA / Auth</option>
                <option value="grok2api">Grok2API</option>
                <option value="sso">SSO</option>
              </Select>
            </div>
            <div className="w-full sm:w-80">
              <label htmlFor="credential-search" className="mb-1.5 block text-xs font-medium text-slate-500">搜索账号</label>
              <div className="relative"><Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" /><Input id="credential-search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索邮箱" className="pl-9" /></div>
            </div>
        </AccountFilterBar>
        <AccountSelectionToolbar
          allVisibleSelected={allVisibleSelected}
          selectableCount={selectableItems.length}
          selectedCount={selectedIds.length}
          total={total}
          loading={loading}
          selectingAll={selectingAll}
          onTogglePage={toggleAllVisible}
          onSelectAll={() => void selectAllAvailable()}
          onClear={() => setSelected({})}
          actions={<Button size="sm" onClick={() => void batchDownload()} disabled={!selectedIds.length || !!busy}>{busy === "download" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Archive className="h-4 w-4" />}批量导出</Button>}
        />

        {loading ? (
          <div className="flex min-h-52 items-center justify-center text-sm text-slate-500"><Loader2 className="mr-2 h-4 w-4 animate-spin" />加载授权文件</div>
        ) : filtered.length ? (
          <>
            <div className="hidden overflow-x-auto md:block">
              <table className="w-full min-w-[1140px] text-left text-sm">
                <thead className="border-b border-slate-200 bg-slate-50 text-xs text-slate-500">
                  <tr>
                    <th className="w-12 px-4 py-3"><span className="sr-only">选择文件</span></th>
                    <th className="px-4 py-3 font-medium">账号</th>
                    <th className="px-4 py-3 font-medium">邮箱来源</th>
                    <th className="w-[160px] px-4 py-3 font-medium">创建时间</th>
                    <th className="px-4 py-3 font-medium">文件状态</th>
                    <th className="px-4 py-3 font-medium">远程状态</th>
                    <th className="px-4 py-3 font-medium">文件路径</th>
                    <th className="px-4 py-3 text-right font-medium">操作</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {filtered.map((item) => {
                    const available = isAvailable(item);
                    const path = tab === "cpa" ? item.cpa_auth_path : tab === "grok2api" ? item.grok2api_auth_path : item.account_file;
                    const remoteStatus = tab === "cpa" ? item.cpa_remote_status : tab === "grok2api" ? item.grok2api_remote_status : "";
                    const remoteLabel = remoteStatus === "success" ? "已导入" : remoteStatus === "failed" ? "失败" : remoteStatus === "partial" ? "同步异常" : remoteStatus === "ready" ? "待导入" : remoteStatus === "not_configured" ? "未配置" : "—";
                    return (
                      <tr key={item.id} className="hover:bg-slate-50/70">
                        <td className="px-4 py-3"><input type="checkbox" disabled={!available} checked={!!selected[item.id]} onChange={(event) => setSelected((old) => ({ ...old, [item.id]: event.target.checked }))} /></td>
                        <td className="max-w-[250px] px-4 py-3">
                          <AccountEmailLabel email={item.email} botRisk={!!item.bot_risk} emailClassName="text-sm text-slate-950" />
                        </td>
                        <td className="px-4 py-3"><EmailProviderLabel provider={item.provider} /></td>
                        <td className="whitespace-nowrap px-4 py-3 text-xs text-slate-500">{item.finished_at || item.started_at || "未记录"}</td>
                        <td className="px-4 py-3"><Badge variant={available ? "success" : "secondary"}>{available ? "有效" : "缺失"}</Badge></td>
                        <td className="px-4 py-3">{tab === "sso" ? <span className="text-slate-400">—</span> : <Badge variant={remoteStatus === "success" ? "success" : remoteStatus === "failed" ? "destructive" : remoteStatus === "not_configured" || !remoteStatus ? "secondary" : "warning"}>{remoteLabel}</Badge>}</td>
                        <td className="max-w-[300px] px-4 py-3"><span className="block truncate text-xs text-slate-500" title={path}>{path || (tab === "sso" ? "未找到账号文件" : "未生成本地文件")}</span></td>
                        <td className="px-4 py-3"><div className="flex justify-end gap-1.5">
                          <Button variant="outline" size="sm" disabled={!available || !!busy} onClick={() => void copyJson(item)}>{busy === `copy-${item.id}` ? <Loader2 className="h-4 w-4 animate-spin" /> : <Copy className="h-4 w-4" />}复制</Button>
                          <a href={available ? api.accountAuthDownloadUrl(item.id, tab) : undefined} download aria-disabled={!available} className={`inline-flex min-h-9 items-center justify-center gap-2 rounded-lg border border-slate-200 px-3 text-xs font-medium ${available ? "bg-white text-slate-700 hover:bg-slate-50" : "pointer-events-none bg-slate-50 text-slate-300"}`}><Download className="h-4 w-4" />下载</a>
                          {tab === "grok2api" && item.grok2api_remote_configured ? <Button variant="outline" size="sm" disabled={!available || !!busy} onClick={() => void importGrok2API(item)}>{busy === `import-${item.id}` ? <Loader2 className="h-4 w-4 animate-spin" /> : <UploadCloud className="h-4 w-4" />}导入</Button> : null}
                        </div></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <div className="divide-y divide-slate-100 md:hidden">
              {filtered.map((item) => {
                const available = isAvailable(item);
                const path = tab === "cpa" ? item.cpa_auth_path : tab === "grok2api" ? item.grok2api_auth_path : item.account_file;
                const remoteStatus = tab === "cpa" ? item.cpa_remote_status : tab === "grok2api" ? item.grok2api_remote_status : "";
                return (
                  <div key={item.id} className="space-y-3 p-4">
                    <div className="flex items-start gap-3"><input type="checkbox" className="mt-1" disabled={!available} checked={!!selected[item.id]} onChange={(event) => setSelected((old) => ({ ...old, [item.id]: event.target.checked }))} /><div className="flex min-w-0 flex-1 items-start gap-2"><AccountEmailLabel email={item.email} botRisk={!!item.bot_risk} className="min-w-0 flex-1" emailClassName="text-sm text-slate-950" /><EmailProviderIcon provider={item.provider} /></div><Badge variant={available ? "success" : "secondary"}>{available ? "有效" : "缺失"}</Badge></div>
                    <div className="text-xs text-slate-500">创建于 {item.finished_at || item.started_at || "未记录"}</div>
                    <div className="grid grid-cols-2 gap-2 text-xs"><div className="rounded-lg bg-slate-50 px-3 py-2"><div className="text-slate-400">文件路径</div><div className="mt-1 truncate text-slate-700" title={path}>{path || "未生成"}</div></div><div className="rounded-lg bg-slate-50 px-3 py-2"><div className="text-slate-400">远程状态</div><div className="mt-1 text-slate-700">{tab === "sso" ? "—" : remoteStatus || "未配置"}</div></div></div>
                    <div className="grid grid-cols-2 gap-2"><Button variant="outline" size="sm" disabled={!available || !!busy} onClick={() => void copyJson(item)}>{busy === `copy-${item.id}` ? <Loader2 className="h-4 w-4 animate-spin" /> : <Copy className="h-4 w-4" />}复制</Button><a href={available ? api.accountAuthDownloadUrl(item.id, tab) : undefined} download aria-disabled={!available} className={`inline-flex min-h-9 items-center justify-center gap-2 rounded-lg border border-slate-200 px-3 text-xs font-medium ${available ? "bg-white text-slate-700 hover:bg-slate-50" : "pointer-events-none bg-slate-50 text-slate-300"}`}><Download className="h-4 w-4" />下载</a>{tab === "grok2api" && item.grok2api_remote_configured ? <Button variant="outline" size="sm" className="col-span-2" disabled={!available || !!busy} onClick={() => void importGrok2API(item)}>{busy === `import-${item.id}` ? <Loader2 className="h-4 w-4 animate-spin" /> : <UploadCloud className="h-4 w-4" />}导入远程</Button> : null}</div>
                  </div>
                );
              })}
            </div>
          </>
        ) : (
          <div className="p-4"><EmptyState title="暂无授权文件" description="账号完成 SSO → Auth 后会在这里显示对应文件。" /></div>
        )}
        {total > 0 ? (
          <PaginationBar
            page={page}
            pageSize={pageSize}
            total={total}
            loading={loading}
            pageSizeOptions={[20, 50, 100, 200]}
            onPageChange={(nextPage) => void load(nextPage)}
            onPageSizeChange={(nextSize) => {
              setPageSize(nextSize);
              setSelected({});
              void load(1, query, nextSize);
            }}
          />
        ) : null}
      </Card>
      <Toast message={toast.message} tone={toast.tone} />
    </div>
  );
}
