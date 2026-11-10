// A Plotly figure from the API, drawn in the active theme – the same figures the NiceGUI and Streamlit pages show.
// Light mode draws the API's figure as it is (transparent background, theme text colour). Dark mode also swaps the
// light-page chart colours named in tokens.json "chart_dark" and draws gridlines, axis lines and hover labels in theme
// colours.
import { useEffect, useRef } from "react";
import Plotly from "plotly.js-dist-min";
import tokens from "../../../design/tokens.json";
import { fontFamily, isDark, palette } from "../theme";

type Figure = { data?: unknown[]; layout?: Record<string, unknown> };
type Layout = Record<string, unknown>;

const norm = (colour: string) => colour.toUpperCase().replace(/ /g, "");
const DARK_COLOURS: Record<string, string> = Object.fromEntries(
  Object.entries(tokens.chart_dark.colours).map(([light, dark]) => [norm(light), dark]),
);

/** Every colour of a figure found in DARK_COLOURS replaced, in data and layout alike. */
function recolour(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(recolour);
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, recolour(v)]));
  if (typeof value === "string") return DARK_COLOURS[norm(value)] ?? value;
  return value;
}

function darkFigure(figure: Figure, colours: Record<string, string>): Figure {
  const data = recolour(figure.data ?? []) as unknown[];
  const layout = recolour(figure.layout ?? {}) as Layout;
  const lines = { gridcolor: colours.border, zerolinecolor: colours.border, linecolor: colours.border };
  const axes = new Set([...Object.keys(layout).filter((k) => /^[xy]axis\d*$/.test(k)), "xaxis", "yaxis"]);
  for (const axis of axes) layout[axis] = { ...((layout[axis] as Layout) ?? {}), ...lines };
  layout.hoverlabel = { ...((layout.hoverlabel as Layout) ?? {}), bgcolor: colours.surface_raised, bordercolor: colours.border, font: { color: colours.text } };
  return { data, layout };
}

export function PlotlyFigure({ figure, height }: { figure: Figure; height?: number }) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const draw = () => {
      const colours = palette();
      const shown = isDark() ? darkFigure(figure, colours) : figure;
      const layout = {
        ...(shown.layout ?? {}),
        paper_bgcolor: "rgba(0,0,0,0)",
        plot_bgcolor: "rgba(0,0,0,0)",
        font: { color: colours.text, family: fontFamily },
        ...(height ? { height } : {}),
      };
      void Plotly.react(el, (shown.data ?? []) as Plotly.Data[], layout, { responsive: true, displaylogo: false });
    };
    draw();
    window.addEventListener("dk-theme", draw);
    return () => {
      window.removeEventListener("dk-theme", draw);
      Plotly.purge(el);
    };
  }, [figure, height]);
  return <div ref={box} style={{ width: "100%" }} />;
}
