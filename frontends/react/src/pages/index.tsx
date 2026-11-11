// Page id -> page component (same ids as the NiceGUI pages). A page not yet built shows
// the placeholder; the parity list in docs/FRONTEND_PARITY.md tracks which ones exist.
import { type ComponentType } from "react";
import { DataQuality } from "./DataQuality";
import { Overview } from "./Overview";
import { Placeholder } from "./Placeholder";
import { Admin } from "./Admin";
import { Approvals } from "./Approvals";
import { Insights } from "./Insights";
import { Projects } from "./Projects";
import { Console } from "./Console";
import { Planning } from "./Planning";
import { RulesPage } from "./Rules";
import { WhatIfComparison } from "./WhatIfComparison";

export const PAGE_IDS = ["overview", "console", "approvals", "projects", "scenarios", "planning", "data_quality", "rules", "admin", "insights"] as const;

const built: Partial<Record<(typeof PAGE_IDS)[number], ComponentType>> = {
  overview: Overview,
  data_quality: DataQuality,
  admin: Admin,
  approvals: Approvals,
  projects: Projects,
  console: Console,
  scenarios: WhatIfComparison,
  planning: Planning,
  rules: RulesPage,
  insights: Insights,
};

export function pageComponent(id: (typeof PAGE_IDS)[number]): ComponentType {
  return built[id] ?? (() => <Placeholder pageId={id} />);
}
