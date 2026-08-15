import { ListChecks, Loader2 } from "lucide-react";
import type { ReactNode } from "react";
import { Badge, Button } from "@/components/ui";

export function AccountFilterBar({
  children,
  actions,
}: {
  children: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="border-b border-slate-200 bg-white p-4 sm:p-5">
      <div className="flex flex-col gap-3 xl:flex-row xl:items-end xl:justify-between">
        <div className="flex min-w-0 flex-1 flex-wrap items-end gap-2">{children}</div>
        {actions ? <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
      </div>
    </div>
  );
}

export function AccountSelectionToolbar({
  allVisibleSelected,
  selectableCount,
  selectedCount,
  total,
  loading = false,
  selectingAll = false,
  onTogglePage,
  onSelectAll,
  onClear,
  selectAllLabel = "全部",
  actions,
}: {
  allVisibleSelected: boolean;
  selectableCount: number;
  selectedCount: number;
  total: number;
  loading?: boolean;
  selectingAll?: boolean;
  onTogglePage: (checked: boolean) => void;
  onSelectAll: () => void;
  onClear: () => void;
  selectAllLabel?: string;
  actions?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-slate-200 bg-slate-50 px-4 py-2.5 text-xs text-slate-600 sm:px-5">
      <label className="inline-flex min-h-9 cursor-pointer items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 font-medium text-slate-700 shadow-sm hover:bg-slate-50">
        <input
          type="checkbox"
          checked={allVisibleSelected}
          disabled={!selectableCount || loading}
          onChange={(event) => onTogglePage(event.target.checked)}
          aria-label="选择本页"
        />
        选择本页
      </label>
      <Badge variant="secondary" className="rounded-lg px-2.5">已选 {selectedCount}</Badge>
      <Button size="sm" variant="outline" disabled={selectingAll || loading || !total} onClick={onSelectAll}>
        {selectingAll ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> : <ListChecks className="h-4 w-4" aria-hidden="true" />}
        {selectAllLabel}{total ? ` ${total} 条` : ""}
      </Button>
      <Button size="sm" variant="ghost" disabled={!selectedCount} onClick={onClear}>取消选择</Button>
      {actions ? <div className="flex w-full flex-wrap items-center gap-2 border-t border-slate-200 pt-2 sm:ml-auto sm:w-auto sm:border-l sm:border-t-0 sm:pl-2 sm:pt-0">{actions}</div> : null}
    </div>
  );
}
