import { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowUpRight, RefreshCw, Sparkles, X } from "lucide-react";
import { useLocation } from "react-router-dom";

import { Badge, Button, buttonVariants } from "@/components/ui";
import { api, type VersionInfo } from "@/lib/api";

const DISMISSED_VERSION_KEY = "grok-register-dismissed-update-version";
const SNAPSHOT_POLL_MS = 5 * 60 * 1000;
const PREVIEW_QUERY_KEY = "preview-update";
const PREVIEW_LATEST_VERSION = "v9.9.9-preview";
export const UPDATE_SNAPSHOT_EVENT = "grok-update-snapshot";

function dismissedVersion() {
  try {
    return window.localStorage.getItem(DISMISSED_VERSION_KEY) || "";
  } catch {
    return "";
  }
}

function rememberDismissedVersion(version: string) {
  try {
    window.localStorage.setItem(DISMISSED_VERSION_KEY, version);
  } catch {
    // 浏览器禁用本地存储时，关闭状态仅保留到本次页面生命周期结束。
  }
}

function previewVersion(currentVersion = "v1.0.0"): VersionInfo {
  return {
    currentVersion,
    latestVersion: PREVIEW_LATEST_VERSION,
    updateAvailable: true,
    status: "update_available",
    checkedAt: new Date().toISOString(),
    releaseUrl: "https://github.com/kaibush/grok-register/releases",
    releaseNotes:
      "这是版本更新弹窗的预览效果。实际发现新版本时，这里会显示对应 Release Notes。",
    error: "",
  };
}

export function UpdateNotice() {
  const location = useLocation();
  const [version, setVersion] = useState<VersionInfo | null>(null);
  const [open, setOpen] = useState(false);
  const [checking, setChecking] = useState(false);
  const previewEnabled = useMemo(
    () => new URLSearchParams(location.search).get(PREVIEW_QUERY_KEY) === "1",
    [location.search],
  );

  const applySnapshot = useCallback((next: VersionInfo, forceOpen = false) => {
    setVersion(next);
    if (
      next.updateAvailable
      && next.latestVersion
      && (forceOpen || dismissedVersion() !== next.latestVersion)
    ) {
      setOpen(true);
    } else {
      setOpen(false);
    }
  }, []);

  useEffect(() => {
    const onSnapshot = (event: Event) => {
      const next = (event as CustomEvent<{ version?: VersionInfo }>).detail?.version;
      if (next) applySnapshot(next, true);
    };
    window.addEventListener(UPDATE_SNAPSHOT_EVENT, onSnapshot);
    return () => window.removeEventListener(UPDATE_SNAPSHOT_EVENT, onSnapshot);
  }, [applySnapshot]);

  const refresh = useCallback(async () => {
    if (previewEnabled) {
      setVersion(previewVersion());
      setOpen(true);
      try {
        const response = await api.versionInfo();
        setVersion(previewVersion(response.version.currentVersion || undefined));
      } catch {
        // 预览模式不依赖版本接口，接口异常时仍展示模拟内容。
      }
      return;
    }
    try {
      let response = await api.versionInfo();
      if (response.version.status === "unchecked") {
        setChecking(true);
        response = await api.checkForUpdates();
      }
      applySnapshot(response.version);
    } catch {
      // 更新检测不影响控制台其它功能；后端会按周期继续尝试。
    } finally {
      setChecking(false);
    }
  }, [applySnapshot, previewEnabled]);

  useEffect(() => {
    void refresh();
    if (previewEnabled) return;
    const timer = window.setInterval(() => void refresh(), SNAPSHOT_POLL_MS);
    return () => window.clearInterval(timer);
  }, [previewEnabled, refresh]);

  useEffect(() => {
    if (!open) return;
    const previousOverflow = document.body.style.overflow;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        if (!previewEnabled && version?.latestVersion) rememberDismissedVersion(version.latestVersion);
        setOpen(false);
      }
    };
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [open, previewEnabled, version?.latestVersion]);

  if (!open || !version?.updateAvailable) return null;

  const close = () => {
    if (!previewEnabled) rememberDismissedVersion(version.latestVersion);
    setOpen(false);
  };

  return (
    <div
      className="fixed inset-0 z-[130] flex items-end bg-slate-950/55 sm:items-center sm:justify-center sm:p-6"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) close();
      }}
    >
      <section
        role="dialog"
        aria-modal="true"
        aria-labelledby="update-notice-title"
        aria-describedby="update-notice-description"
        className="w-full overflow-hidden rounded-t-3xl bg-white shadow-2xl sm:max-w-lg sm:rounded-3xl"
      >
        <div className="mx-auto mt-2 h-1.5 w-12 rounded-full bg-slate-300 sm:hidden" />
        <header className="flex items-start justify-between gap-4 border-b border-slate-100 px-5 py-5 sm:px-6">
          <div className="flex min-w-0 items-start gap-3">
            <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-2xl bg-sky-50 text-sky-600">
              <Sparkles className="h-5 w-5" aria-hidden="true" />
            </span>
            <div className="min-w-0">
              <h2 id="update-notice-title" className="text-lg font-semibold tracking-tight text-slate-950">
                发现注册机新版本
              </h2>
              <p id="update-notice-description" className="mt-1 text-sm leading-6 text-slate-500">
                新版本已发布，可查看更新说明后安排升级。
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={close}
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-slate-500 hover:bg-slate-100 hover:text-slate-900"
            aria-label="关闭更新提示"
          >
            <X className="h-5 w-5" aria-hidden="true" />
          </button>
        </header>

        <div className="space-y-4 px-5 py-5 sm:px-6">
          <div className="flex flex-wrap items-center gap-2">
            {previewEnabled ? <Badge variant="warning">预览模式</Badge> : null}
            <Badge variant="secondary">当前 {version.currentVersion}</Badge>
            <span className="text-xs text-slate-400">→</span>
            <Badge variant="success">最新 {version.latestVersion}</Badge>
          </div>

          {version.releaseNotes ? (
            <div className="max-h-52 overflow-y-auto whitespace-pre-wrap break-words rounded-xl bg-slate-50 px-4 py-3 text-xs leading-6 text-slate-600">
              {version.releaseNotes}
            </div>
          ) : (
            <div className="rounded-xl bg-slate-50 px-4 py-3 text-xs leading-6 text-slate-500">
              发布页中提供本次版本的更新信息。
            </div>
          )}

          <div className="rounded-xl border border-sky-100 bg-sky-50/70 px-4 py-3">
            <div className="flex items-center gap-2 text-xs font-medium text-sky-900">
              <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
              Docker 部署更新命令
            </div>
            <code className="mt-2 block break-all rounded-lg bg-white/80 px-3 py-2 text-[11px] leading-5 text-slate-700 ring-1 ring-sky-100">
              docker compose pull &amp;&amp; docker compose up -d --force-recreate
            </code>
          </div>
        </div>

        <footer className="flex gap-2 border-t border-slate-100 px-5 pb-[calc(1rem+env(safe-area-inset-bottom))] pt-4 sm:px-6 sm:pb-5">
          <Button variant="outline" className="flex-1" onClick={close}>
            关闭
          </Button>
          {version.releaseUrl ? (
            <a
              href={version.releaseUrl}
              target="_blank"
              rel="noreferrer"
              className={buttonVariants({ className: "flex-1" })}
            >
              查看更新
              <ArrowUpRight className="h-4 w-4" aria-hidden="true" />
            </a>
          ) : null}
        </footer>
        {checking ? <span className="sr-only">正在检查更新</span> : null}
      </section>
    </div>
  );
}
