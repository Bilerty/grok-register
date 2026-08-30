import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { ArrowUpRight, RefreshCw, Sparkles, X } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
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
    releaseNotes: [
      "## 更新内容",
      "",
      "- 账号中心新增 **出口 IP 风控** 页面，可查看和删除风控名单",
      "- 账号详情显示该次注册的出口 IP",
      "- Grok2API 自动导入支持分别选择 `grok_build` / `grok_web` / `grok_console`",
      "",
      "完整说明见 [Release 页面](https://github.com/kaibush/grok-register/releases)。",
    ].join("\n"),
    error: "",
  };
}


function markdownLink({ href, children }: { href?: string; children?: ReactNode }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className="font-medium text-sky-700 underline decoration-sky-200 underline-offset-2 hover:text-sky-800"
    >
      {children}
    </a>
  );
}

function ReleaseNotesMarkdown({ markdown }: { markdown: string }) {
  return (
    <div className="max-h-[min(22rem,50vh)] overflow-y-auto break-words rounded-xl bg-slate-50 px-4 py-3 text-xs leading-6 text-slate-600">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          h1: ({ children }) => <h3 className="mb-2 text-sm font-semibold text-slate-900">{children}</h3>,
          h2: ({ children }) => <h3 className="mb-2 text-sm font-semibold text-slate-900">{children}</h3>,
          h3: ({ children }) => <h4 className="mb-1.5 text-xs font-semibold text-slate-800">{children}</h4>,
          p: ({ children }) => <p className="mb-2 last:mb-0">{children}</p>,
          ul: ({ children }) => <ul className="mb-2 list-disc space-y-1 pl-4 last:mb-0">{children}</ul>,
          ol: ({ children }) => <ol className="mb-2 list-decimal space-y-1 pl-4 last:mb-0">{children}</ol>,
          li: ({ children }) => <li className="break-words">{children}</li>,
          strong: ({ children }) => <strong className="font-semibold text-slate-800">{children}</strong>,
          em: ({ children }) => <em className="italic">{children}</em>,
          hr: () => <hr className="my-3 border-slate-200" />,
          blockquote: ({ children }) => (
            <blockquote className="mb-2 border-l-2 border-slate-300 pl-3 text-slate-500 last:mb-0">{children}</blockquote>
          ),
          a: markdownLink,
          code: ({ className, children }) => (
            className ? (
              <code className="font-mono text-[11px] text-slate-800">{children}</code>
            ) : (
              <code className="rounded bg-white px-1 py-0.5 font-mono text-[11px] text-slate-800 ring-1 ring-slate-200">{children}</code>
            )
          ),
          pre: ({ children }) => (
            <pre className="mb-2 overflow-x-auto rounded-lg bg-white px-3 py-2 ring-1 ring-slate-200 last:mb-0">{children}</pre>
          ),
          table: ({ children }) => (
            <div className="mb-2 overflow-x-auto last:mb-0">
              <table className="w-full border-collapse text-left">{children}</table>
            </div>
          ),
          th: ({ children }) => <th className="border-b border-slate-200 px-2 py-1 font-semibold text-slate-800">{children}</th>,
          td: ({ children }) => <td className="border-b border-slate-100 px-2 py-1">{children}</td>,
          img: ({ alt }) => <span className="italic text-slate-400">[{alt || "图片"}]</span>,
        }}
      >
        {markdown}
      </ReactMarkdown>
    </div>
  );
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
        className="w-full overflow-hidden rounded-t-3xl bg-white shadow-2xl sm:max-w-xl sm:rounded-3xl"
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
            <ReleaseNotesMarkdown markdown={version.releaseNotes} />
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
