// The look from the shared design tokens (frontends/design/tokens.json): CSS variables for light and dark, the
// stored light/dark choice (default: the device setting) and the tone of a verdict, RAG status or environment.
import tokens from "../../design/tokens.json";

type Palette = Record<string, string>;
const vars = (palette: Palette) =>
  Object.entries(palette).map(([name, value]) => `--dk-${name.replace(/_/g, "-")}: ${value};`).join(" ");

export function installTheme(): void {
  const style = document.createElement("style");
  style.textContent = `
:root { ${vars(tokens.light)} --dk-font: ${tokens.font.family}; --dk-radius-card: ${tokens.radius.card};
  --dk-radius-control: ${tokens.radius.control}; --dk-page-x: ${tokens.space.page_x}; --dk-max-width: ${tokens.space.max_width}; }
:root[data-theme="dark"] { ${vars(tokens.dark)} }
@media (max-width: ${tokens.breakpoints.mobile}px) { :root { --dk-page-x: ${tokens.space.page_x_mobile}; } }`;
  document.head.prepend(style);
  applyThemeChoice();
}

const KEY = "dk.theme";
export type ThemeChoice = "light" | "dark" | "system";

export function themeChoice(): ThemeChoice {
  try {
    return (localStorage.getItem(KEY) as ThemeChoice) || "system";
  } catch {
    return "system";
  }
}

export function isDark(): boolean {
  const choice = themeChoice();
  return choice === "dark" || (choice === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
}

export function applyThemeChoice(): void {
  document.documentElement.dataset.theme = isDark() ? "dark" : "light";
}

export function setThemeChoice(choice: ThemeChoice): void {
  try {
    localStorage.setItem(KEY, choice);
  } catch {
    /* private mode: the choice lasts for this page only */
  }
  applyThemeChoice();
  window.dispatchEvent(new Event("dk-theme"));
}

export function palette(): Palette {
  return isDark() ? tokens.dark : tokens.light;
}

export type Tone = "success" | "warning" | "danger" | "info" | "neutral";
export function tone(kind: "verdict" | "rag" | "environment", value: string | null | undefined): Tone {
  const table = tokens[kind] as Record<string, string>;
  return ((value && table[value]) || "neutral") as Tone;
}

export const fontFamily = tokens.font.family;
