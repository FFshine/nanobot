import { useState } from "react";
import { ChevronDown, ChevronRight, LogOut, Settings, Shield, User } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { UserGroup } from "@/lib/types";
import { cn } from "@/lib/utils";

interface UserMenuProps {
  username: string;
  displayName: string;
  role: "admin" | "user";
  groups: UserGroup[];
  onOpenSettings: () => void;
  onLogout: () => void;
}

export function UserMenu({
  username,
  displayName,
  role,
  groups,
  onOpenSettings,
  onLogout,
}: UserMenuProps) {
  const { t } = useTranslation();
  const [groupsOpen, setGroupsOpen] = useState(false);

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          className={cn(
            "flex items-center gap-1.5 rounded-full px-2 py-1 text-[12px] font-medium",
            "text-muted-foreground/85 hover:bg-accent/40 hover:text-foreground",
            "transition-colors",
          )}
        >
          <span className="flex h-5 w-5 items-center justify-center rounded-full bg-muted">
            <User className="h-3 w-3" />
          </span>
          <span className="max-w-[8rem] truncate">{displayName || username}</span>
          <ChevronDown className="h-3 w-3 opacity-50" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-56">
        <DropdownMenuLabel className="font-normal">
          <div className="flex flex-col gap-0.5">
            <span className="font-medium text-foreground">{displayName}</span>
            <span className="text-[11px] text-muted-foreground">@{username}</span>
          </div>
          <span
            className={cn(
              "mt-1.5 inline-flex h-5 items-center rounded-full px-2 text-[10px] font-semibold",
              role === "admin"
                ? "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400"
                : "bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400",
            )}
          >
            {role === "admin" ? "Admin" : t("settings.readOnly", "Read only")}
          </span>
        </DropdownMenuLabel>

        {groups.length > 0 && (
          <>
            <DropdownMenuSeparator />
            <button
              type="button"
              onClick={() => setGroupsOpen((v) => !v)}
              className="flex w-full items-center gap-1 px-2 py-1 text-[11px] font-medium text-muted-foreground hover:text-foreground transition-colors"
            >
              {groupsOpen ? (
                <ChevronDown className="h-3 w-3 shrink-0" />
              ) : (
                <ChevronRight className="h-3 w-3 shrink-0" />
              )}
              {t("userMenu.groups", "Groups")}
              <span className="ml-auto text-[10px] text-muted-foreground/60">
                {groups.length}
              </span>
            </button>
            {groupsOpen &&
              groups.map((g) => (
                <div
                  key={g.id}
                  className="flex items-center gap-2 px-2 py-1 text-[12px]"
                >
                  <Shield className="h-3 w-3 shrink-0 text-muted-foreground" />
                  <span className="truncate">{g.displayName || g.name}</span>
                  {g.role === "admin" && (
                    <span className="ml-auto shrink-0 text-[9px] font-medium text-muted-foreground/60">
                      admin
                    </span>
                  )}
                </div>
              ))}
          </>
        )}

        <DropdownMenuSeparator />
        <DropdownMenuItem
          onClick={onOpenSettings}
          className="gap-2 text-[12px]"
        >
          <Settings className="h-3.5 w-3.5" />
          {t("sidebar.settings")}
        </DropdownMenuItem>
        <DropdownMenuItem
          onClick={onLogout}
          className="gap-2 text-[12px] text-destructive focus:text-destructive"
        >
          <LogOut className="h-3.5 w-3.5" />
          {t("app.account.logout")}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
