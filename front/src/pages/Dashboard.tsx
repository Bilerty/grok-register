import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  Activity,
  ArrowUpRight,
  CheckCircle2,
  Clock3,
  Database,
  FileJson2,
  Play,
  RefreshCw,
  ServerCog,
  Users,
} from "lucide-react";
import { api, type JobStatus, type Stats } from "@/lib/api";
import {
  Badge,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  PageHeader,
  buttonVariants,
} from "@/components/ui";

const shortcuts = [
  { to: "/registration/new", label: "新建注册", hint: "设置数量与并发", icon: Play },
  { to: "/accounts", label: "账号列表", hint: "筛选和管理结果", icon: Users },
  { to: "/accounts/relogin", label: "重新登录", hint: "批量刷新授权", icon: RefreshCw },
  { to: "/settings/config", label: "查看配置", hint: "核对实际配置文件", icon: FileJson2 },
];

function formatSuccessRate(success = 0, total = 0) {
  if (total <= 0) return "—";
  return `${Math.round((success / total) * 100)}%`;
}

export function DashboardPage() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [job, setJob] = useState<JobStatus | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  const refresh = async (manual = false) => {
    if (manual) setRefreshing(true);
    try {
      const data = await api.stats();
      setStats(data.stats);
      setJob(data.job);
      setError("");
    } catch (reason: any) {
      setError(reason.message || "数据加载失败");
    } finally {
      setLoading(false);
      if (manual) setRefreshing(false);
    }
  };

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 4000);
    return () => window.clearInterval(timer);
  }, []);

  const maxProviderTotal = useMemo(
    () => Math.max(1, ...(stats?.providers || []).map((item) => item.total || 0)),
    [stats?.providers]
  );

  return (
    <div className="space-y-5 sm:space-y-6">
      <PageHeader
        title="工作概览"
        description="集中查看注册任务、账号结果与外部服务状态。"
        actions={
          <>
            <Button variant="outline" onClick={() => void refresh(true)} disabled={refreshing}>
              <RefreshCw className={`h-4 w-4 ${refreshing ? "animate-spin" : ""}`} aria-hidden="true" />
              刷新数据
            </Button>
            <Link to="/registration/new" className={buttonVariants()}>
              <Play className="h-4 w-4" aria-hidden="true" />
              新建注册
            </Link>
          </>
        }
      />

      {error ? <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800">{error}</div> : null}

      <Card className="overflow-hidden" aria-label="核心统计">
        <section className="grid grid-cols-2 divide-x divide-y divide-slate-200 md:grid-cols-4 md:divide-y-0">
          {[
            { title: "成功账号", value: stats?.unique_success_emails ?? 0, hint: `成功记录 ${stats?.success ?? 0} 条`, icon: CheckCircle2, tone: "bg-emerald-50 text-emerald-600" },
            { title: "全部记录", value: stats?.total ?? 0, hint: `失败 ${stats?.failure ?? 0} · 跳过 ${stats?.skipped ?? 0}`, icon: Database, tone: "bg-sky-50 text-sky-600" },
            { title: "今日完成", value: stats?.today_success ?? 0, hint: `今日任务记录 ${stats?.today_total ?? 0}`, icon: Clock3, tone: "bg-slate-100 text-slate-600" },
            { title: "CPA 入库", value: stats?.cpa_success ?? 0, hint: `失败 ${stats?.cpa_failed ?? 0} · 邮箱停用 ${stats?.email_disabled ?? 0}`, icon: ServerCog, tone: "bg-amber-50 text-amber-600" },
          ].map((item) => {
            const Icon = item.icon;
            return <div key={item.title} className="min-w-0 p-4 sm:p-5">
              <div className="flex items-center gap-2 text-xs font-medium text-slate-500"><span className={`flex h-7 w-7 items-center justify-center rounded-full ${item.tone}`}><Icon className="h-3.5 w-3.5" /></span>{item.title}</div>
              <div className="mt-3 text-2xl font-semibold tabular-nums tracking-tight text-slate-950">{loading ? "…" : item.value}</div>
              <div className="mt-1 text-xs text-slate-400">{item.hint}</div>
            </div>;
          })}
        </section>
        <section className="grid grid-cols-1 divide-y divide-slate-200 border-t border-slate-200 sm:grid-cols-2 sm:divide-x sm:divide-y-0" aria-label="注册成功率">
          {[
            { title: "今日注册成功率", value: formatSuccessRate(stats?.today_success, stats?.today_total), hint: `成功 ${stats?.today_success ?? 0} / 今日记录 ${stats?.today_total ?? 0}`, tone: "text-sky-700" },
            { title: "总注册成功率", value: formatSuccessRate(stats?.success, stats?.total), hint: `成功 ${stats?.success ?? 0} / 总记录 ${stats?.total ?? 0}`, tone: "text-emerald-700" },
          ].map((item) => (
            <div key={item.title} className="flex items-center justify-between gap-4 bg-slate-50/50 px-4 py-3 sm:px-5">
              <div className="min-w-0">
                <div className="text-xs font-medium text-slate-600">{item.title}</div>
                <div className="mt-1 truncate text-xs text-slate-400">{item.hint}</div>
              </div>
              <div className={`shrink-0 text-2xl font-semibold tabular-nums tracking-tight ${item.tone}`}>{loading ? "…" : item.value}</div>
            </div>
          ))}
        </section>
      </Card>

      <section className="grid gap-4 xl:grid-cols-[minmax(0,1.35fr)_minmax(320px,.75fr)]">
        <Card>
          <CardHeader className="flex-row items-start justify-between gap-4 border-b border-slate-100">
            <div>
              <CardTitle>当前注册任务</CardTitle>
              <CardDescription>任务进度与执行状态会自动更新。</CardDescription>
            </div>
            <Badge variant={job?.running ? "warning" : "success"}>{job?.running ? "执行中" : "空闲"}</Badge>
          </CardHeader>
          <CardContent className="space-y-5 pt-5 sm:pt-5">
            <div className="grid grid-cols-3 divide-x divide-slate-200 rounded-xl border border-slate-200 bg-slate-50/60 py-4 text-center">
              {[["目标账号", job?.target_count ?? 0], ["并发数", job?.workers ?? 1], ["日志行", job?.log_count ?? 0]].map(([label, value]) => (
                <div key={label} className="px-2">
                  <div className="text-xl font-semibold tabular-nums text-slate-950 sm:text-2xl">{value}</div>
                  <div className="mt-1 text-xs text-slate-500">{label}</div>
                </div>
              ))}
            </div>
            <div className={`flex items-start gap-3 rounded-xl border px-4 py-3 text-sm leading-6 ${job?.last_error ? "border-red-200 bg-red-50 text-red-800" : "border-slate-200 bg-white text-slate-600"}`}>
              <Activity className={`mt-1 h-4 w-4 shrink-0 ${job?.last_error ? "text-red-600" : "text-sky-600"}`} />
              <span>{job?.last_error || (job?.running ? "任务正在执行，可在运行监控中查看实时日志与进度。" : "当前没有运行中的任务，可以创建注册任务或处理已有账号。")}</span>
            </div>
            <div className="flex flex-wrap gap-2">
              <Link to={job?.running ? "/registration/runtime" : "/registration/new"} className={buttonVariants({ variant: "secondary" })}>{job?.running ? "打开运行监控" : "创建任务"}</Link>
              <Link to="/accounts" className={buttonVariants({ variant: "outline" })}>查看账号</Link>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="border-b border-slate-100">
            <CardTitle>服务商分布</CardTitle>
            <CardDescription>成功数量与全部记录对比</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4 pt-5 sm:pt-5">
            {(stats?.providers || []).length === 0 ? <p className="py-8 text-center text-sm text-slate-500">暂无服务商数据</p> : (stats?.providers || []).slice(0, 6).map((item) => (
              <div key={item.provider || "unknown"} className="space-y-2">
                <div className="flex items-center justify-between gap-3 text-sm">
                  <span className="truncate font-medium text-slate-800">{item.provider || "未知"}</span>
                  <span className="tabular-nums text-slate-500"><strong className="font-semibold text-slate-900">{item.success}</strong> / {item.total}</span>
                </div>
                <div className="h-1.5 overflow-hidden rounded-full bg-slate-100"><div className="h-full rounded-full bg-sky-500" style={{ width: `${Math.max(4, item.total / maxProviderTotal * 100)}%` }} /></div>
              </div>
            ))}
          </CardContent>
        </Card>
      </section>

      <section>
        <div className="mb-3 flex items-center justify-between">
          <div><h2 className="text-base font-semibold text-slate-950">常用功能</h2><p className="mt-1 text-sm text-slate-500">按工作流程快速进入独立功能页。</p></div>
        </div>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {shortcuts.map((item) => {
            const Icon = item.icon;
            return <Link key={item.to} to={item.to} className="group flex items-center gap-3 rounded-xl border border-slate-200 bg-white p-4 transition hover:border-slate-300 hover:bg-slate-50">
              <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-slate-100 text-slate-700"><Icon className="h-4.5 w-4.5" /></span>
              <span className="min-w-0 flex-1"><span className="block text-sm font-medium text-slate-900">{item.label}</span><span className="mt-0.5 block truncate text-xs text-slate-500">{item.hint}</span></span>
              <ArrowUpRight className="h-4 w-4 text-slate-400 group-hover:text-sky-600" />
            </Link>;
          })}
        </div>
      </section>
    </div>
  );
}
