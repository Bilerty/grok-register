import {
  Archive,
  FileJson2,
  LayoutDashboard,
  Mail,
  MonitorDot,
  PlaySquare,
  RefreshCcw,
  ShieldAlert,
  ShieldCheck,
  Settings2,
  SlidersHorizontal,
  Users,
  type LucideIcon,
} from "lucide-react";

export type NavigationItem = {
  to: string;
  label: string;
  shortLabel: string;
  icon: LucideIcon;
};

export type NavigationGroup = {
  label: string;
  items: readonly NavigationItem[];
};

export const accountNavigationItems: readonly NavigationItem[] = [
  { to: "/accounts", label: "账号管理", shortLabel: "账号", icon: Users },
  { to: "/accounts/sso-check", label: "SSO 风控检查", shortLabel: "风控", icon: ShieldCheck },
  { to: "/accounts/exit-ips", label: "出口 IP 风控", shortLabel: "出口IP", icon: ShieldAlert },
  { to: "/accounts/relogin", label: "账号重新登录", shortLabel: "重登", icon: RefreshCcw },
  { to: "/accounts/credentials", label: "授权文件管理", shortLabel: "授权", icon: Archive },
];

export const navigationGroups: readonly NavigationGroup[] = [
  {
    label: "工作台",
    items: [{ to: "/overview", label: "概览", shortLabel: "概览", icon: LayoutDashboard }],
  },
  {
    label: "注册中心",
    items: [
      { to: "/registration/new", label: "新建注册", shortLabel: "注册", icon: PlaySquare },
      { to: "/registration/runtime", label: "运行监控", shortLabel: "监控", icon: MonitorDot },
    ],
  },
  {
    label: "账号中心",
    items: accountNavigationItems,
  },
  {
    label: "系统配置",
    items: [
      { to: "/settings/registration", label: "注册设置", shortLabel: "设置", icon: SlidersHorizontal },
      { to: "/settings/mail", label: "邮箱服务", shortLabel: "邮箱", icon: Mail },
      { to: "/settings/outlook", label: "Outlook 邮箱池", shortLabel: "Outlook", icon: Settings2 },
      { to: "/settings/config", label: "配置文件", shortLabel: "配置", icon: FileJson2 },
    ],
  },
];

export const navigationItems: readonly NavigationItem[] = navigationGroups.flatMap((group) => group.items);

export const mobilePrimaryItems: readonly NavigationItem[] = [
  navigationGroups[0].items[0],
  navigationGroups[1].items[0],
  navigationGroups[2].items[0],
];
