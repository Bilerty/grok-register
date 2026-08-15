import { useEffect, useMemo, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { Activity, Database, LogOut, Menu, MoreHorizontal, PanelLeftClose, PanelLeftOpen, RefreshCw, X } from "lucide-react";
import { mobilePrimaryItems, navigationGroups, navigationItems } from "@/app/navigation";
import { UPDATE_SNAPSHOT_EVENT, UpdateNotice } from "@/components/UpdateNotice";
import { Toast } from "@/components/ui";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

function navigationActive(pathname: string, to: string) {
  if (to === "/accounts") return pathname === to;
  return pathname === to || pathname.startsWith(`${to}/`);
}

function StatusPill({ running, compact = false }: { running?: boolean; compact?: boolean }) {
  return (
    <div
      className={cn(
        "inline-flex min-h-8 items-center gap-2 rounded-lg border px-2.5 text-xs font-medium",
        running
          ? "border-amber-200 bg-amber-50 text-amber-800"
          : "border-emerald-200 bg-emerald-50 text-emerald-800"
      )}
    >
      <span className={cn("h-1.5 w-1.5 rounded-full", running ? "animate-pulse bg-amber-500" : "bg-emerald-500")} />
      {compact ? (running ? "运行中" : "空闲") : running ? "注册任务运行中" : "系统空闲"}
    </div>
  );
}

function Brand() {
  return (
    <div className="flex min-w-0 items-center gap-3">
      <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-slate-900 text-xs font-bold tracking-wide text-white">
        GR
      </div>
      <div className="min-w-0">
        <div className="truncate text-sm font-semibold tracking-tight text-slate-950">Grok Register</div>
        <div className="truncate text-[11px] text-slate-500">账号与授权控制台</div>
      </div>
    </div>
  );
}

function NavigationContent({ onNavigate, collapsed = false }: { onNavigate?: () => void; collapsed?: boolean }) {
  const location = useLocation();
  return (
    <nav className="flex flex-col gap-5" aria-label="主导航">
      {navigationGroups.map((group) => (
        <section key={group.label}>
          {collapsed ? <div className="mx-2 mb-2 border-t border-slate-100" /> : <div className="mb-1.5 px-3 text-[11px] font-medium uppercase tracking-[0.08em] text-slate-400">{group.label}</div>}
          <div className={cn("space-y-0.5", group.label === "账号中心" && !collapsed && "border-l border-slate-200 pl-2")}>
            {group.items.map((item) => {
              const Icon = item.icon;
              const active = navigationActive(location.pathname, item.to);
              return (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.to === "/accounts"}
                  onClick={onNavigate}
                  title={collapsed ? item.label : undefined}
                  aria-current={active ? "page" : undefined}
                  className={cn(
                    "relative flex min-h-10 items-center gap-3 rounded-lg px-3 text-sm transition-colors",
                    collapsed && "justify-center px-2",
                    active
                      ? "bg-sky-50 font-medium text-sky-700"
                      : "text-slate-600 hover:bg-slate-50 hover:text-slate-950"
                  )}
                >
                  <Icon className="h-4 w-4 shrink-0" strokeWidth={1.8} aria-hidden="true" />
                  <span className={cn("truncate", collapsed && "sr-only")}>{item.label}</span>
                </NavLink>
              );
            })}
          </div>
        </section>
      ))}
    </nav>
  );
}

export function Layout({ jobRunning, onLogout }: { jobRunning?: boolean; onLogout?: () => void }) {
  const location = useLocation();
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const [checkingUpdate, setCheckingUpdate] = useState(false);
  const [updateToast, setUpdateToast] = useState<{ message: string; tone: "success" | "error" }>({
    message: "",
    tone: "success",
  });
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => {
    try {
      return window.localStorage.getItem("grok-sidebar-collapsed") === "1";
    } catch {
      return false;
    }
  });
  const current = useMemo(
    () =>
      [...navigationItems]
        .sort((a, b) => b.to.length - a.to.length)
        .find((item) => location.pathname === item.to || location.pathname.startsWith(`${item.to}/`)),
    [location.pathname]
  );
  const primaryActive = mobilePrimaryItems.some(
    (item) => navigationActive(location.pathname, item.to)
  );

  useEffect(() => setMobileMenuOpen(false), [location.pathname]);
  useEffect(() => {
    try {
      window.localStorage.setItem("grok-sidebar-collapsed", sidebarCollapsed ? "1" : "0");
    } catch {
      // 浏览器禁用本地存储时只保留当前会话状态。
    }
  }, [sidebarCollapsed]);
  useEffect(() => {
    document.body.style.overflow = mobileMenuOpen ? "hidden" : "";
    return () => {
      document.body.style.overflow = "";
    };
  }, [mobileMenuOpen]);
  useEffect(() => {
    if (!updateToast.message) return;
    const timer = window.setTimeout(
      () => setUpdateToast((current) => ({ ...current, message: "" })),
      4000,
    );
    return () => window.clearTimeout(timer);
  }, [updateToast.message]);

  const checkForUpdates = async () => {
    if (checkingUpdate) return;
    setCheckingUpdate(true);
    try {
      const response = await api.checkForUpdates();
      const version = response.version;
      window.dispatchEvent(
        new CustomEvent(UPDATE_SNAPSHOT_EVENT, { detail: { version } }),
      );
      if (version.status === "check_failed") {
        setUpdateToast({ message: version.error || "检查更新失败", tone: "error" });
      } else if (version.updateAvailable) {
        setUpdateToast({ message: `发现新版本 ${version.latestVersion}`, tone: "success" });
      } else {
        setUpdateToast({
          message: `当前已是最新版本 ${version.currentVersion}`,
          tone: "success",
        });
      }
    } catch (error) {
      setUpdateToast({
        message: error instanceof Error ? error.message : "检查更新失败",
        tone: "error",
      });
    } finally {
      setCheckingUpdate(false);
    }
  };

  return (
    <div className="min-h-[100dvh] bg-[#f7f8f7] text-foreground">
      <UpdateNotice />
      <Toast message={updateToast.message} tone={updateToast.tone} />
      <a
        href="#main-content"
        className="fixed left-3 top-3 z-[100] -translate-y-24 rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white transition-transform focus:translate-y-0"
      >
        跳到主要内容
      </a>

      <header className="fixed inset-x-0 top-0 z-50 flex h-12 items-center justify-between border-b border-slate-200 bg-white px-4 sm:px-5">
        <div className="flex items-center gap-2">
          <Brand />
          <button
            type="button"
            className="ml-2 hidden h-8 w-8 items-center justify-center rounded-lg border border-slate-200 text-slate-500 hover:bg-slate-50 hover:text-slate-900 lg:flex"
            onClick={() => setSidebarCollapsed((value) => !value)}
            aria-label={sidebarCollapsed ? "展开侧栏" : "折叠侧栏"}
            title={sidebarCollapsed ? "展开侧栏" : "折叠侧栏"}
          >
            {sidebarCollapsed ? <PanelLeftOpen className="h-4 w-4" /> : <PanelLeftClose className="h-4 w-4" />}
          </button>
        </div>
        <div className="flex items-center gap-2 text-xs text-slate-500">
          <button
            type="button"
            onClick={() => void checkForUpdates()}
            disabled={checkingUpdate}
            className="flex h-8 shrink-0 items-center justify-center gap-1.5 rounded-lg border border-slate-200 bg-white px-2.5 font-medium text-slate-600 hover:bg-slate-50 hover:text-slate-950 disabled:cursor-wait disabled:opacity-60"
            aria-label={checkingUpdate ? "正在检查更新" : "检查更新"}
            title={checkingUpdate ? "正在检查更新" : "检查更新"}
          >
            <RefreshCw className={cn("h-3.5 w-3.5", checkingUpdate && "animate-spin")} aria-hidden="true" />
            <span className="hidden sm:inline">{checkingUpdate ? "检查中" : "检查更新"}</span>
          </button>
          <span className="hidden sm:inline">本地控制台</span>
          <StatusPill running={jobRunning} compact />
        </div>
      </header>

      <aside className={cn("fixed inset-y-0 left-0 top-12 z-40 hidden flex-col border-r border-slate-200 bg-white transition-[width] duration-200 lg:flex", sidebarCollapsed ? "w-[68px]" : "w-[208px]")}>
        <div className={cn("flex-1 overflow-y-auto py-5", sidebarCollapsed ? "px-2" : "px-3")}>
          <NavigationContent collapsed={sidebarCollapsed} />
        </div>
        <div className={cn("border-t border-slate-100", sidebarCollapsed ? "p-2" : "p-3")}>
          {sidebarCollapsed ? (
            <div className="space-y-2">
              <div className="flex h-10 items-center justify-center rounded-lg bg-slate-50" title={jobRunning ? "注册任务运行中" : "系统空闲"}>
                <span className={cn("h-2 w-2 rounded-full", jobRunning ? "animate-pulse bg-amber-500" : "bg-emerald-500")} />
              </div>
              {onLogout ? <button type="button" onClick={onLogout} className="flex h-10 w-full items-center justify-center rounded-lg border border-slate-200 bg-white text-slate-600 hover:bg-slate-50" aria-label="退出登录" title="退出登录"><LogOut className="h-4 w-4" /></button> : null}
            </div>
          ) : <div className="rounded-xl bg-slate-50 p-3">
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs text-slate-500">运行状态</span>
              <StatusPill running={jobRunning} compact />
            </div>
            <div className="mt-3 flex items-center gap-2 text-xs text-slate-500">
              <Database className="h-3.5 w-3.5 text-sky-600" aria-hidden="true" />
              SQLite 已连接
            </div>
            {onLogout ? (
              <button
                type="button"
                onClick={onLogout}
                className="mt-3 flex min-h-9 w-full items-center justify-center gap-2 rounded-lg border border-slate-200 bg-white px-3 text-xs font-medium text-slate-700 hover:bg-slate-50"
              >
                <LogOut className="h-3.5 w-3.5" aria-hidden="true" />
                退出登录
              </button>
            ) : null}
          </div>}
        </div>
      </aside>

      <div className={cn("min-w-0 pt-12 transition-[padding] duration-200", sidebarCollapsed ? "lg:pl-[68px]" : "lg:pl-[208px]")}>
        <header className="sticky top-12 z-30 flex min-h-14 items-center justify-between border-b border-slate-200 bg-white/95 px-4 backdrop-blur lg:hidden">
          <div className="flex min-w-0 items-center gap-3">
            <button
              type="button"
              className="flex h-9 w-9 items-center justify-center rounded-lg border border-slate-200 bg-white lg:hidden"
              onClick={() => setMobileMenuOpen(true)}
              aria-label="打开导航"
            >
              <Menu className="h-4 w-4" aria-hidden="true" />
            </button>
            <div className="hidden lg:block">
              <div className="flex items-center gap-2 text-xs text-slate-400">
                <span>控制台</span>
                <span>/</span>
                <span className="font-medium text-slate-700">{current?.label || "概览"}</span>
              </div>
            </div>
            <div className="min-w-0 lg:hidden">
              <div className="truncate text-sm font-semibold text-slate-950">{current?.label || "工作台"}</div>
              <div className="truncate text-[11px] text-slate-500">Grok Register</div>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <StatusPill running={jobRunning} compact />
            <div className="hidden items-center gap-2 rounded-lg border border-slate-200 px-2.5 py-1.5 text-xs text-slate-500 sm:flex">
              <Activity className="h-3.5 w-3.5 text-sky-600" aria-hidden="true" />
              服务正常
            </div>
            {onLogout ? (
              <button
                type="button"
                onClick={onLogout}
                className="hidden min-h-9 items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 text-xs font-medium text-slate-700 hover:bg-slate-50 sm:flex"
              >
                <LogOut className="h-3.5 w-3.5" aria-hidden="true" />
                退出
              </button>
            ) : null}
          </div>
        </header>

        <main
          id="main-content"
          className="w-full px-4 pb-[calc(5.5rem+env(safe-area-inset-bottom))] pt-5 sm:px-6 sm:pt-6 lg:px-5 lg:pb-10 lg:pt-6 xl:px-6"
        >
          <Outlet />
        </main>
      </div>

      {mobileMenuOpen ? (
        <div className="fixed inset-0 z-[80] lg:hidden">
          <button
            type="button"
            className="absolute inset-0 bg-slate-950/35"
            onClick={() => setMobileMenuOpen(false)}
            aria-label="关闭导航"
          />
          <aside className="absolute inset-y-0 left-0 flex w-[min(88vw,320px)] flex-col bg-white shadow-2xl">
            <div className="flex items-center justify-between border-b border-slate-100 px-5 py-4">
              <Brand />
              <button
                type="button"
                className="flex h-9 w-9 items-center justify-center rounded-lg border border-slate-200"
                onClick={() => setMobileMenuOpen(false)}
                aria-label="关闭导航"
              >
                <X className="h-4 w-4" aria-hidden="true" />
              </button>
            </div>
            <div className="flex-1 overflow-y-auto px-3 py-5">
              <NavigationContent onNavigate={() => setMobileMenuOpen(false)} />
            </div>
            {onLogout ? (
              <div className="border-t border-slate-100 p-3">
                <button
                  type="button"
                  onClick={onLogout}
                  className="flex min-h-10 w-full items-center justify-center gap-2 rounded-lg border border-slate-200 text-sm font-medium"
                >
                  <LogOut className="h-4 w-4" aria-hidden="true" />
                  退出登录
                </button>
              </div>
            ) : null}
          </aside>
        </div>
      ) : null}

      <nav
        className="fixed inset-x-0 bottom-0 z-50 grid grid-cols-4 border-t border-slate-200 bg-white/96 px-1 pb-[env(safe-area-inset-bottom)] shadow-[0_-8px_24px_-20px_rgba(15,23,42,0.3)] backdrop-blur lg:hidden"
        aria-label="手机端主导航"
      >
        {mobilePrimaryItems.map((item) => {
          const Icon = item.icon;
          const active = navigationActive(location.pathname, item.to);
          return (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/accounts"}
              aria-current={active ? "page" : undefined}
              className={cn(
                "flex min-h-[62px] flex-col items-center justify-center gap-1 text-[11px] font-medium",
                active ? "text-sky-600" : "text-slate-500"
              )}
            >
              <Icon className="h-5 w-5" strokeWidth={1.8} aria-hidden="true" />
              <span>{item.shortLabel}</span>
            </NavLink>
          );
        })}
        <button
          type="button"
          onClick={() => setMobileMenuOpen(true)}
          className={cn(
            "flex min-h-[62px] flex-col items-center justify-center gap-1 text-[11px] font-medium",
            !primaryActive ? "text-sky-600" : "text-slate-500"
          )}
        >
          <MoreHorizontal className="h-5 w-5" strokeWidth={1.8} aria-hidden="true" />
          <span>更多</span>
        </button>
      </nav>
    </div>
  );
}
