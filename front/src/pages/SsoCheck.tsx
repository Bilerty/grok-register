import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  AlertTriangle,
  CheckCircle2,
  Clock3,
  History,
  ListChecks,
  Loader2,
  Search,
  ShieldAlert,
  ShieldCheck,
  Trash2,
  X,
} from "lucide-react";
import { AccountPageContext } from "@/components/AccountPageContext";
import { AccountEmailLabel, EmailProviderIcon, EmailProviderLabel } from "@/components/AccountEmailIcon";
import { AccountFilterBar, AccountSelectionToolbar } from "@/components/AccountTableToolbar";
import { Badge, Button, Card, EmptyState, Input, PageHeader, PaginationBar, Select, Toast } from "@/components/ui";
import { api, type AccountRecord, type SsoCheckItem, type SsoCheckStatus } from "@/lib/api";
import {
  appendSsoCheckHistory,
  clearSsoCheckHistory,
  loadSsoCheckHistory,
  removeSsoCheckHistory,
  type SsoCheckHistoryEntry,
} from "@/lib/ssoCheckHistory";

function statusLabel(status: SsoCheckItem["status"]) {
  return { clean: "正常", flagged: "异常", unknown: "未知", failed: "失败", pending: "等待" }[status];
}

function statusVariant(status: SsoCheckItem["status"]) {
  if (status === "clean") return "success" as const;
  if (status === "flagged" || status === "failed") return "destructive" as const;
  if (status === "unknown") return "warning" as const;
  return "secondary" as const;
}

function sourceLabel(value: number | string | null) {
  return value === null || value === "" ? "未知" : String(value);
}

function resultNote(item: SsoCheckItem) {
  if (item.status === "clean") return "botFlagSource=0";
  if (item.status === "flagged") return `botFlagSource=${sourceLabel(item.bot_flag_source)}`;
  if (item.status === "pending") return "等待检查";
  return item.error || "未读取到稳定的 botFlagSource";
}

function formatWhen(value: number | null | undefined) {
  return value ? new Date(value * 1000).toLocaleString() : "时间未知";
}

const SSO_RESULT_PAGE_SIZE = 20;

function accountSsoStatus(item: AccountRecord) {
  if (!item.sso_available) return { label: "缺失", variant: "secondary" as const };
  if (item.bot_risk) return { label: "异常", variant: "destructive" as const };
  if (item.sso_risk_check?.bot_flag_source === 0 || item.sso_risk_check?.bot_flag_source === "0") return { label: "正常", variant: "success" as const };
  if (item.sso_risk_check) return { label: "未知", variant: "warning" as const };
  return { label: "未检查", variant: "secondary" as const };
}

function SsoResultTable({ items }: { items: SsoCheckItem[] }) {
  return (
    <>
      <div className="hidden overflow-x-auto md:block">
        <table className="w-full min-w-[620px] text-left text-sm">
          <thead className="border-b border-slate-200 bg-slate-50 text-xs text-slate-500">
            <tr>
              <th className="px-4 py-3 font-medium">账号</th>
              <th className="px-4 py-3 font-medium">结果</th>
              <th className="px-4 py-3 font-medium">说明</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {items.map((item) => (
              <tr key={item.account_id} className={item.status === "flagged" ? "bg-red-50/35" : "hover:bg-slate-50/70"}>
                <td className="px-4 py-3"><AccountEmailLabel email={item.email || `账号 #${item.account_id}`} botRisk={item.status === "flagged"} /></td>
                <td className="px-4 py-3"><Badge variant={statusVariant(item.status)}>{statusLabel(item.status)}</Badge></td>
                <td className="px-4 py-3 text-xs text-slate-500">
                  <div className={item.status === "flagged" || item.status === "failed" ? "break-words text-red-700" : "break-words"}>{resultNote(item)}</div>
                  <div className="mt-1 text-[11px] text-slate-400">{item.response_ms ? `${item.response_ms} ms` : "耗时未知"}{Number(item.attempts || 0) > 1 ? ` · 检查 ${item.attempts} 次` : ""}</div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="divide-y divide-slate-100 md:hidden">
        {items.map((item) => (
          <div key={item.account_id} className="space-y-3 p-4">
            <div className="flex items-start justify-between gap-3">
              <AccountEmailLabel email={item.email || `账号 #${item.account_id}`} botRisk={item.status === "flagged"} />
              <Badge variant={statusVariant(item.status)}>{statusLabel(item.status)}</Badge>
            </div>
            <div className="rounded-lg bg-slate-50 px-3 py-2 text-xs text-slate-600">
              <div className={item.status === "flagged" || item.status === "failed" ? "break-words text-red-700" : "break-words"}>{resultNote(item)}</div>
              <div className="mt-1 text-[11px] text-slate-400">{item.response_ms ? `${item.response_ms} ms` : "耗时未知"}{Number(item.attempts || 0) > 1 ? ` · 检查 ${item.attempts} 次` : ""}</div>
            </div>
          </div>
        ))}
      </div>
    </>
  );
}

function SsoResultsDrawer({
  status,
  items,
  page,
  onPageChange,
  onClose,
}: {
  status: SsoCheckStatus;
  items: SsoCheckItem[];
  page: number;
  onPageChange: (page: number) => void;
  onClose: () => void;
}) {
  const totalPages = Math.max(1, Math.ceil(status.items.length / SSO_RESULT_PAGE_SIZE));
  return (
    <div className="fixed inset-0 z-[100] flex justify-end">
      <button
        type="button"
        tabIndex={-1}
        className="absolute inset-0 bg-slate-950/45"
        aria-label="关闭本次检查结果"
        onClick={onClose}
      />
      <aside
        role="dialog"
        aria-modal="true"
        aria-labelledby="sso-results-title"
        aria-describedby="sso-results-description"
        className="relative flex h-full w-full max-w-4xl flex-col border-l border-slate-200 bg-white shadow-2xl"
      >
        <header className="flex shrink-0 items-start justify-between gap-3 border-b border-slate-200 px-4 py-4 sm:px-5">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h2 id="sso-results-title" className="font-semibold text-slate-950">本次检查结果</h2>
              <Badge variant={status.running ? "default" : "success"}>{status.running ? "检查中" : "已完成"}</Badge>
            </div>
            <p id="sso-results-description" className="mt-1 text-xs text-slate-500">
              共 {status.items.length} 个账号 · 每页 20 条 · 第 {page} / {totalPages} 页
            </p>
            <div className="mt-2 flex flex-wrap gap-1.5">
              <Badge variant="success">正常 {status.clean_count}</Badge>
              <Badge variant="destructive">异常 {status.flagged_count}</Badge>
              <Badge variant="warning">未知 {status.unknown_count}</Badge>
              {status.failed_count ? <Badge variant="destructive">失败 {status.failed_count}</Badge> : null}
            </div>
          </div>
          <Button size="icon" variant="ghost" className="shrink-0" onClick={onClose} aria-label="关闭本次检查结果">
            <X className="h-5 w-5" aria-hidden="true" />
          </Button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto">
          <SsoResultTable items={items} />
        </div>
        <div className="shrink-0 bg-white pb-[env(safe-area-inset-bottom)]">
          <PaginationBar
            page={page}
            pageSize={SSO_RESULT_PAGE_SIZE}
            total={status.items.length}
            onPageChange={onPageChange}
          />
        </div>
      </aside>
    </div>
  );
}

export function SsoCheckPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [accounts, setAccounts] = useState<AccountRecord[]>([]);
  const [selected, setSelected] = useState<Record<number, boolean>>({});
  const [query, setQuery] = useState("");
  const [riskFilter, setRiskFilter] = useState("");
  const [loading, setLoading] = useState(true);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [total, setTotal] = useState(0);
  const [selectingAll, setSelectingAll] = useState(false);
  const [starting, setStarting] = useState(false);
  const [reloginRunning, setReloginRunning] = useState(false);
  const [status, setStatus] = useState<SsoCheckStatus | null>(null);
  const [resultPage, setResultPage] = useState(1);
  const [resultsOpen, setResultsOpen] = useState(false);
  const [toast, setToast] = useState<{ message: string; tone?: "default" | "success" | "error" }>({ message: "" });
  const recordedRun = useRef("");
  const preparedSelectionApplied = useRef(false);

  const notify = (message: string, tone: "default" | "success" | "error" = "default") => {
    setToast({ message, tone });
    window.setTimeout(() => setToast({ message: "" }), 2400);
  };
  const load = async (targetPage = page, targetQuery = query, targetPageSize = pageSize) => {
    setLoading(true);
    try {
      const result = await api.accounts({
        limit: targetPageSize,
        offset: (targetPage - 1) * targetPageSize,
        q: targetQuery.trim(),
        botRisk: riskFilter || undefined,
      });
      setAccounts(result.items || []);
      setTotal(Number(result.total ?? result.items?.length ?? 0));
      setPage(targetPage);
    } catch (error: any) { notify(error.message || "账号加载失败", "error"); }
    finally { setLoading(false); }
  };

  useEffect(() => {
    const preservePreparedSelection = searchParams.get("prepared") === "1";
    const timer = window.setTimeout(() => {
      if (!preservePreparedSelection) setSelected({});
      void load(1, query);
    }, 280);
    return () => window.clearTimeout(timer);
  }, [query, riskFilter]);
  useEffect(() => {
    if (preparedSelectionApplied.current) return;
    if (searchParams.get("prepared") !== "1") return;
    preparedSelectionApplied.current = true;
    let ids: number[] = [];
    try {
      const raw = JSON.parse(window.sessionStorage.getItem("grok-sso-check-selection") || "[]");
      ids = Array.isArray(raw) ? raw.map(Number).filter((id) => Number.isInteger(id) && id > 0) : [];
    } catch {
      ids = [];
    }
    window.sessionStorage.removeItem("grok-sso-check-selection");
    const note = window.sessionStorage.getItem("grok-sso-check-selection-note") || "";
    window.sessionStorage.removeItem("grok-sso-check-selection-note");
    setSearchParams({}, { replace: true });
    if (ids.length) {
      setSelected(Object.fromEntries(ids.map((id) => [id, true])));
      notify(`已带入 ${ids.length} 个账号，请确认后开始检查${note ? `；${note}` : ""}`, "success");
    }
  }, [searchParams, setSearchParams]);
  useEffect(() => {
    let active = true;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const response = await api.ssoCheckStatus();
        if (!active) return;
        const next = response.sso_check;
        setStatus(next);
        if (next.running) { timer = window.setTimeout(poll, 1500); return; }
        if (next.run_id && recordedRun.current !== next.run_id) {
          await appendSsoCheckHistory(next);
          recordedRun.current = next.run_id;
          if (next.finished_at) void load();
        }
      } catch { if (active) timer = window.setTimeout(poll, 4000); }
    };
    void poll();
    return () => { active = false; if (timer) window.clearTimeout(timer); };
  }, [status?.running]);

  useEffect(() => {
    let active = true;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const result = await api.reloginStatus();
        if (!active) return;
        setReloginRunning(!!result.relogin.running);
        if (result.relogin.running) timer = window.setTimeout(poll, 2500);
      } catch {
        if (active) timer = window.setTimeout(poll, 5000);
      }
    };
    void poll();
    return () => {
      active = false;
      if (timer) window.clearTimeout(timer);
    };
  }, []);

  useEffect(() => {
    setResultPage(1);
  }, [status?.run_id]);

  useEffect(() => {
    if (!resultsOpen) return;
    const previousOverflow = document.body.style.overflow;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setResultsOpen(false);
    };
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", closeOnEscape);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", closeOnEscape);
    };
  }, [resultsOpen]);

  const eligible = accounts.filter((item) => item.sso_available);
  const selectedIds = Object.entries(selected).filter(([, checked]) => checked).map(([id]) => Number(id));
  const allSelected = eligible.length > 0 && eligible.every((item) => selected[item.id]);
  const progress = status?.total_count ? Math.round(status.completed_count / status.total_count * 100) : 0;
  const visibleResults = status?.items || [];
  const safeResultPage = Math.min(resultPage, Math.max(1, Math.ceil(visibleResults.length / SSO_RESULT_PAGE_SIZE)));
  const pagedResults = visibleResults.slice((safeResultPage - 1) * SSO_RESULT_PAGE_SIZE, safeResultPage * SSO_RESULT_PAGE_SIZE);

  const start = async () => {
    if (!selectedIds.length || status?.running) return;
    if (!window.confirm(`详细检查选中的 ${selectedIds.length} 个账号 SSO 风控状态？`)) return;
    setStarting(true);
    try {
      const result = await api.startSsoCheck(selectedIds);
      recordedRun.current = "";
      setResultsOpen(false);
      setStatus(result.sso_check);
      notify("SSO 详细检查已启动", "success");
    } catch (error: any) { notify(error.message || "启动检查失败", "error"); }
    finally { setStarting(false); }
  };

  const selectAllMatchingAccounts = async () => {
    setSelectingAll(true);
    try {
      const result = await api.actionableAccountIds("sso_check", query.trim(), riskFilter);
      const ids = result.ids || [];
      setSelected(Object.fromEntries(ids.map((id) => [id, true])));
      notify(`已选择 ${ids.length} 个可检查账号`, "success");
    } catch (error: any) {
      notify(error.message || "选择全部账号失败", "error");
    } finally {
      setSelectingAll(false);
    }
  };

  return (
    <div className="space-y-5">
      <AccountPageContext crumbs={[{ label: "SSO 风控" }]} />
      <PageHeader
        title="SSO 风控检查"
        description="批量读取已保存 SSO 并检查 botFlag；0 为正常，非 0 为异常，空值会按 0 / 2 / 4 / 8 秒自动复查。"
        actions={<><Button variant="outline" disabled={!visibleResults.length} onClick={() => setResultsOpen(true)}><ListChecks className="h-4 w-4" />本次结果{visibleResults.length ? ` (${status?.completed_count || 0}/${status?.total_count || visibleResults.length})` : ""}</Button><Link to="/accounts/sso-check/history" className="inline-flex min-h-10 items-center justify-center gap-2 rounded-lg border border-slate-200 bg-white px-4 text-sm font-medium text-slate-700 hover:bg-slate-50"><History className="h-4 w-4" />检查历史</Link></>}
      />

      {status?.running ? (
        <Card className="border-sky-200 p-4 sm:p-5">
          <div className="flex items-start justify-between gap-3"><div><div className="flex items-center gap-2 font-semibold"><Loader2 className="h-4 w-4 animate-spin text-sky-600" />正在检查</div><p className="mt-1 text-xs text-slate-500">{status.email || "准备账号"} · {status.stage}</p></div><Badge>{status.completed_count}/{status.total_count}</Badge></div>
          <div className="mt-4 h-2 overflow-hidden rounded-full bg-slate-100"><div className="h-full bg-sky-500" style={{ width: `${progress}%` }} /></div>
        </Card>
      ) : status?.run_id && status.finished_at ? (
        <Card className="flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:justify-between sm:p-5"><div><div className="font-semibold">最近一次检查已完成</div><div className="mt-1 text-sm text-slate-500">正常 {status.clean_count} · 异常 {status.flagged_count} · 未知 {status.unknown_count} · 失败 {status.failed_count}</div></div><Link to={`/accounts/sso-check/history/${status.run_id}`} className="inline-flex min-h-10 items-center justify-center rounded-lg bg-slate-900 px-4 text-sm font-medium text-white">查看报告</Link></Card>
      ) : null}

      <Card className="overflow-hidden">
        <AccountFilterBar>
          <div className="w-full sm:w-48"><label htmlFor="sso-risk-filter" className="mb-1.5 block text-xs font-medium text-slate-500">风控状态</label><Select id="sso-risk-filter" value={riskFilter} onChange={(event) => { setRiskFilter(event.target.value); setSelected({}); }} aria-label="按账号风控状态筛选"><option value="">全部账号</option><option value="0">正常账号</option><option value="1">异常账号</option><option value="unknown">未检查 / 未知</option></Select></div>
          <div className="w-full sm:w-80"><label htmlFor="sso-account-search" className="mb-1.5 block text-xs font-medium text-slate-500">搜索账号</label><div className="relative"><Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" /><Input id="sso-account-search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索邮箱" className="pl-9" /></div></div>
        </AccountFilterBar>
        <AccountSelectionToolbar
          allVisibleSelected={allSelected}
          selectableCount={eligible.length}
          selectedCount={selectedIds.length}
          total={total}
          loading={loading}
          selectingAll={selectingAll}
          onTogglePage={(checked) => setSelected((old) => { const next = { ...old }; for (const item of eligible) checked ? (next[item.id] = true) : delete next[item.id]; return next; })}
          onSelectAll={() => void selectAllMatchingAccounts()}
          onClear={() => setSelected({})}
          actions={<Button size="sm" onClick={() => void start()} disabled={!selectedIds.length || starting || !!status?.running || reloginRunning}>{starting ? <Loader2 className="h-4 w-4 animate-spin" /> : <ShieldCheck className="h-4 w-4" />}开始检查</Button>}
        />
        {reloginRunning ? <p className="border-b border-amber-200 bg-amber-50 px-4 py-2 text-xs text-amber-700 sm:px-5">账号重新登录正在运行，完成后可启动 SSO 风控检查。</p> : null}
        {loading ? <div className="flex min-h-48 items-center justify-center text-sm text-slate-500"><Loader2 className="mr-2 h-4 w-4 animate-spin" />加载账号</div> : accounts.length ? (
          <>
            <div className="hidden overflow-x-auto md:block">
              <table className="w-full min-w-[980px] text-left text-sm">
                <thead className="border-b border-slate-200 bg-slate-50 text-xs text-slate-500"><tr><th className="w-12 px-4 py-3"><span className="sr-only">选择账号</span></th><th className="px-4 py-3 font-medium">账号</th><th className="px-4 py-3 font-medium">邮箱来源</th><th className="w-[160px] px-4 py-3 font-medium">创建时间</th><th className="px-4 py-3 font-medium">SSO 文件</th><th className="px-4 py-3 font-medium">风控结果</th><th className="px-4 py-3 font-medium">检查信息</th></tr></thead>
                <tbody className="divide-y divide-slate-100">
                  {accounts.map((item) => { const risk = accountSsoStatus(item); return <tr key={item.id} className={`hover:bg-slate-50/70 ${!item.sso_available ? "opacity-60" : ""}`}><td className="px-4 py-3"><input type="checkbox" disabled={!item.sso_available} checked={!!selected[item.id]} onChange={(event) => setSelected((old) => ({ ...old, [item.id]: event.target.checked }))} /></td><td className="max-w-[280px] px-4 py-3"><AccountEmailLabel email={item.email} botRisk={!!item.bot_risk} /></td><td className="px-4 py-3"><EmailProviderLabel provider={item.provider} /></td><td className="whitespace-nowrap px-4 py-3 text-xs text-slate-500">{item.finished_at || item.started_at || "未记录"}</td><td className="px-4 py-3"><Badge variant={item.sso_available ? "success" : "secondary"}>{item.sso_available ? "有效" : "缺失"}</Badge></td><td className="px-4 py-3"><Badge variant={risk.variant}>{risk.label}</Badge></td><td className="max-w-[280px] px-4 py-3 text-xs text-slate-500"><span className="block truncate" title={item.sso_risk_check?.error || item.account_file || ""}>{item.sso_risk_check ? resultNote({ account_id: item.id, email: item.email, status: item.bot_risk ? "flagged" : item.sso_risk_check.bot_flag_source === 0 ? "clean" : "unknown", bot_flag_source: item.sso_risk_check.bot_flag_source, error: item.sso_risk_check.error, response_ms: item.sso_risk_check.response_ms, attempts: 1 }) : item.sso_available ? "等待检查" : "未找到 SSO 文件"}</span></td></tr>; })}
                </tbody>
              </table>
            </div>
            <div className="divide-y divide-slate-100 md:hidden">
              {accounts.map((item) => { const risk = accountSsoStatus(item); return <label key={item.id} className={`flex items-start gap-3 p-4 ${!item.sso_available ? "opacity-60" : ""}`}><input type="checkbox" className="mt-1" disabled={!item.sso_available} checked={!!selected[item.id]} onChange={(event) => setSelected((old) => ({ ...old, [item.id]: event.target.checked }))} /><div className="min-w-0 flex-1"><div className="flex items-start justify-between gap-3"><div className="flex min-w-0 flex-1 items-start gap-2"><AccountEmailLabel email={item.email} botRisk={!!item.bot_risk} className="min-w-0 flex-1" /><EmailProviderIcon provider={item.provider} /></div><Badge variant={risk.variant}>{risk.label}</Badge></div><div className="mt-1 text-xs text-slate-500">创建于 {item.finished_at || item.started_at || "未记录"}</div><div className="mt-2 flex flex-wrap gap-1.5"><Badge variant={item.sso_available ? "success" : "secondary"}>SSO {item.sso_available ? "有效" : "缺失"}</Badge>{item.sso_risk_check ? <span className="text-xs text-slate-500">{resultNote({ account_id: item.id, email: item.email, status: item.bot_risk ? "flagged" : item.sso_risk_check.bot_flag_source === 0 ? "clean" : "unknown", bot_flag_source: item.sso_risk_check.bot_flag_source, error: item.sso_risk_check.error, response_ms: item.sso_risk_check.response_ms, attempts: 1 })}</span> : null}</div></div></label>; })}
            </div>
          </>
        ) : <div className="p-4"><EmptyState title="暂无账号" description="账号保存 SSO 后即可在这里执行详细检查。" /></div>}
        {total > 0 ? <PaginationBar page={page} pageSize={pageSize} total={total} loading={loading} onPageChange={(next) => void load(next)} onPageSizeChange={(size) => { setPageSize(size); setSelected({}); void load(1, query, size); }} /> : null}
      </Card>

      {resultsOpen && status && visibleResults.length ? <SsoResultsDrawer status={status} items={pagedResults} page={safeResultPage} onPageChange={setResultPage} onClose={() => setResultsOpen(false)} /> : null}
      <Toast message={toast.message} tone={toast.tone} />
    </div>
  );
}

export function SsoCheckHistoryPage() {
  const { runId } = useParams();
  const navigate = useNavigate();
  const [entries, setEntries] = useState<SsoCheckHistoryEntry[]>([]);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<"all" | "clean" | "flagged" | "unknown">("all");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  useEffect(() => { void loadSsoCheckHistory().then(setEntries); }, []);
  const selected = entries.find((entry) => entry.run_id === runId) || null;
  const reportItems = useMemo(() => {
    if (!selected) return [];
    const keyword = query.trim().toLowerCase();
    return selected.items.filter((item) => {
      if (filter === "clean" && item.status !== "clean") return false;
      if (filter === "flagged" && item.status !== "flagged") return false;
      if (filter === "unknown" && !["unknown", "failed"].includes(item.status)) return false;
      return !keyword || item.email.toLowerCase().includes(keyword);
    });
  }, [selected, query, filter]);
  const safePage = Math.min(page, Math.max(1, Math.ceil(reportItems.length / pageSize)));
  const paged = reportItems.slice((safePage - 1) * pageSize, safePage * pageSize);
  const remove = async (id: string) => { if (!window.confirm("删除这次 SSO 检查报告？")) return; setEntries(await removeSsoCheckHistory(id)); if (runId === id) navigate("/accounts/sso-check/history", { replace: true }); };

  if (runId) return (
    <div className="space-y-5">
      <AccountPageContext crumbs={[{ label: "SSO 风控", to: "/accounts/sso-check" }, { label: "检查历史", to: "/accounts/sso-check/history" }, { label: "报告" }]} backTo="/accounts/sso-check/history" backLabel="返回历史列表" />
      {!selected ? <Card className="p-5"><EmptyState title="报告不存在" description="该报告可能已删除或浏览器数据已清理。" /></Card> : <>
        <PageHeader title="SSO 风控报告" description={`完成于 ${formatWhen(selected.finished_at)}`} actions={<Button variant="outline" className="text-red-700" onClick={() => void remove(selected.run_id)}><Trash2 className="h-4 w-4" />删除</Button>} />
        <section className="grid grid-cols-2 gap-3 lg:grid-cols-4">{[["账号", selected.total_count, "text-slate-950", CheckCircle2], ["正常", selected.clean_count, "text-emerald-700", ShieldCheck], ["异常", selected.flagged_count, "text-red-700", ShieldAlert], ["未知 / 失败", selected.unknown_count + selected.failed_count, "text-amber-700", AlertTriangle]].map(([label, value, tone, Icon]: any) => <Card key={label} className="p-4"><div className="flex items-center justify-between"><span className="text-xs text-slate-500">{label}</span><Icon className={`h-4 w-4 ${tone}`} /></div><div className={`mt-2 text-2xl font-semibold tabular-nums ${tone}`}>{value}</div></Card>)}</section>
        <Card className="overflow-hidden"><div className="flex flex-col gap-3 border-b border-slate-200 p-4 sm:flex-row"><div className="relative flex-1"><Search className="pointer-events-none absolute left-3 top-3.5 h-4 w-4 text-slate-400" /><Input value={query} onChange={(event) => { setQuery(event.target.value); setPage(1); }} placeholder="搜索邮箱" className="pl-9" /></div><Select value={filter} onChange={(event) => { setFilter(event.target.value as typeof filter); setPage(1); }} className="sm:w-40" aria-label="筛选风控结果"><option value="all">全部结果</option><option value="clean">仅正常</option><option value="flagged">仅异常</option><option value="unknown">未知 / 失败</option></Select></div><SsoResultTable items={paged} />{reportItems.length ? <PaginationBar page={safePage} pageSize={pageSize} total={reportItems.length} onPageChange={setPage} onPageSizeChange={(size) => { setPageSize(size); setPage(1); }} /> : null}</Card>
      </>}
    </div>
  );

  return (
    <div className="space-y-5">
      <AccountPageContext crumbs={[{ label: "SSO 风控", to: "/accounts/sso-check" }, { label: "检查历史" }]} />
      <PageHeader title="SSO 检查历史" description="报告保存在当前浏览器，保留账号与风控关键信息。" actions={<Button variant="outline" className="text-red-700" disabled={!entries.length} onClick={async () => { if (!window.confirm("清空全部 SSO 检查历史？")) return; setEntries(await clearSsoCheckHistory()); }}><Trash2 className="h-4 w-4" />清空历史</Button>} />
      {entries.length ? <section className="grid gap-3 xl:grid-cols-2">{entries.map((entry) => <Card key={entry.run_id} className="p-4 sm:p-5"><div className="flex items-start justify-between gap-3"><div><div className="flex items-center gap-2 text-sm font-semibold"><Clock3 className="h-4 w-4 text-sky-600" />{formatWhen(entry.finished_at)}</div><div className="mt-3 flex flex-wrap gap-1.5"><Badge variant="secondary">总数 {entry.total_count}</Badge><Badge variant="success">正常 {entry.clean_count}</Badge>{entry.flagged_count ? <Badge variant="destructive">异常 {entry.flagged_count}</Badge> : null}{entry.unknown_count + entry.failed_count ? <Badge variant="warning">未知/失败 {entry.unknown_count + entry.failed_count}</Badge> : null}</div></div><Button size="icon" variant="ghost" className="h-9 w-9 text-red-700" onClick={() => void remove(entry.run_id)}><Trash2 className="h-4 w-4" /></Button></div><Link to={`/accounts/sso-check/history/${entry.run_id}`} className="mt-4 inline-flex min-h-9 w-full items-center justify-center rounded-lg bg-slate-900 px-3 text-xs font-medium text-white">查看报告</Link></Card>)}</section> : <Card className="p-4"><EmptyState title="暂无检查历史" description="完成一次批量 SSO 详细检查后会在这里生成精简报告。" /></Card>}
    </div>
  );
}
